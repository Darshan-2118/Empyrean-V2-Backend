"""
Celery application instance for async task processing.

Broker:    Redis (configured via REDIS_URL)
Backend:   none — task results are ignored (L61), nothing reads them
Beat:      Scheduled tasks defined below

Task modules under ``tasks/`` are imported eagerly via ``include`` so their
``@celery_app.task``-decorated callables register with the worker.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
import threading
from datetime import datetime, timezone

from celery import Celery
from celery.schedules import crontab
from celery.app.task import Task
from sqlalchemy.exc import OperationalError

from config import get_config

cfg = get_config()

logger = logging.getLogger(__name__)

# ── Celery storage directory (keeps root folder clean of schedule files) ────
_CELERY_DIR = Path(__file__).resolve().parent / ".celery"
try:
    _CELERY_DIR.mkdir(exist_ok=True)
except OSError:  # L60: best-effort — import must not die on a read-only rootfs
    pass
_BEAT_SCHEDULE_FILENAME = str(_CELERY_DIR / "celerybeat-schedule")

# ── Redis-backed circuit breaker (#15, M1/M2) ────────────────────────────────
# Tracks failed task attempts per task in a *real* rolling window (M1): each
# failure is one uniquely-membered entry in a per-task sorted set, scored by
# unix timestamp. Pruning uses ZREMRANGEBYSCORE so individual failures age out
# naturally — no more wholesale counter reset on a stale timestamp. Every
# multi-step operation (prune+count, prune+add) runs as a single Lua script so
# concurrent workers can never interleave reads and writes (M2).
#
# M6: the breaker is enforced inside the shared ``Task`` base's
# ``apply_async``, so *every* dispatch entry point (.delay(), .apply_async(),
# send_task, and beat) goes through the same gate — not just ``send_task`` as
# before.
_FAILED_ATTEMPTS_KEY = "celery:circuit_breaker:failed_attempts"
_MAX_FAILED_ATTEMPTS = 10  # After 10 failures, circuit opens
_ROLLING_WINDOW_SIZE = 300  # Count failures within the last 300s (5 minutes)

# Prune expired failures, record this failure (unique member), refresh the
# key TTL, and return the live window size — atomically.
_RECORD_FAILURE_LUA = """
local cutoff = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return redis.call('ZCARD', KEYS[1])
"""

# Prune expired failures and return how many remain — atomically. Empty sets
# are deleted so idle tasks leave no keys behind.
_CHECK_CIRCUIT_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if count == 0 then
    redis.call('DEL', KEYS[1])
end
return count
"""

_lock = threading.Lock()
_enabled = True
_redis_client = None  # Lazily-initialised, reused for the process lifetime


def _get_redis_client():
    """Return a module-level Redis client, creating it once on first call."""
    global _redis_client
    if _redis_client is None:
        with _lock:
            if _redis_client is None:  # double-checked locking
                from redis import Redis
                _redis_client = Redis.from_url(
                    cfg.REDIS_URL,
                    decode_responses=False,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
    return _redis_client


def _circuit_for_task(task_name: str) -> bool:
    """Check if retries should be allowed for *task_name*.

    The circuit is open when the task has accumulated >= _MAX_FAILED_ATTEMPTS
    failures within the last _ROLLING_WINDOW_SIZE seconds. The prune+count
    check runs as one atomic Lua script (M2), so two workers checking
    simultaneously always agree on the state.
    """
    if not _enabled:
        return True

    key = f"{_FAILED_ATTEMPTS_KEY}:{task_name}"
    cutoff = int(datetime.now(timezone.utc).timestamp()) - _ROLLING_WINDOW_SIZE

    try:
        from redis.exceptions import ResponseError

        client = _get_redis_client()
        try:
            count = int(client.eval(_CHECK_CIRCUIT_LUA, 1, key, cutoff))
        except ResponseError as exc:
            # Deployments upgrading from the old hash-based breaker may still
            # carry hash-typed keys under this prefix; start those windows clean.
            if "WRONGTYPE" not in str(exc):
                raise
            client.delete(key)
            count = int(client.eval(_CHECK_CIRCUIT_LUA, 1, key, cutoff))

        if count >= _MAX_FAILED_ATTEMPTS:
            logger.warning(
                "Circuit open for task '%s' - %d failures within %ds rolling "
                "window, skipping dispatch",
                task_name,
                count,
                _ROLLING_WINDOW_SIZE,
            )
            return False
        return True

    except Exception:
        logger.warning("Redis error checking circuit breaker for task %s (fail-open)", task_name)
        return True  # Fail open on Redis errors


class _OpenCircuitError(RuntimeError):
    """Raised when a task is dispatched while its circuit is open."""


def _raise_open_circuit(task_name: str) -> None:
    """Log and raise the open-circuit error for *task_name* (M6)."""
    logger.error(
        "Task '%s' skipped due to circuit being OPEN (%d failures within %ds "
        "rolling window). Trigger reset via: python -c "
        "'from celery_app import reset_circuit_breaker; "
        "reset_circuit_breaker(\"%s\")'",
        task_name,
        _MAX_FAILED_ATTEMPTS,
        _ROLLING_WINDOW_SIZE,
        task_name,
    )
    raise _OpenCircuitError(f"Circuit breaker open for task {task_name}")


class CircuitBreakerTask(Task):
    """Celery Task base that gates every dispatch entry point on the breaker.

    Overriding ``apply_async`` means ``.delay()``, ``.apply_async()`` and
    ``send_task`` all funnel through ``_circuit_for_task`` (M6), so direct task
    calls from worker/beat code are protected exactly like the old
    ``send_task`` gate.

    M82 dispatch contract: on a CLOSED circuit these behave exactly like
    Celery's implementations and return an ``AsyncResult``. On an OPEN
    circuit they **raise** :class:`CircuitBreakerOpenError` (exported below)
    instead of returning — callers that dispatch synchronously must catch it
    (the MQTT dispatch path does). The signature mirrors Celery's own
    ``delay(*args, **kwargs)`` → ``apply_async(args=…, kwargs=…)`` mapping,
    so both positional and keyword task arguments forward correctly.
    """

    def apply_async(self, args=None, kwargs=None, task_id=None, producer=None,
                    link=None, link_error=None, shadow=None, **options):
        if _circuit_for_task(self.name):
            return super().apply_async(
                args=args, kwargs=kwargs, task_id=task_id, producer=producer,
                link=link, link_error=link_error, shadow=shadow, **options
            )
        _raise_open_circuit(self.name)

    def delay(self, *args, **kwargs):
        return self.apply_async(args=args, kwargs=kwargs)


def _record_task_failure(task_name: str) -> None:
    """Record a failed task execution for circuit breaker tracking (#15).

    Called automatically by all tasks that use autoretry_for. Each failure is
    one uniquely-membered sorted-set entry scored by unix time; the
    prune+add+TTL sequence runs atomically via Lua (M2) so concurrent workers
    recording failures can never lose counts.
    """
    if not _enabled:
        return

    now = int(datetime.now(timezone.utc).timestamp())
    key = f"{_FAILED_ATTEMPTS_KEY}:{task_name}"
    member = f"{now}:{uuid.uuid4().hex}"
    ttl_ms = (_ROLLING_WINDOW_SIZE + 60) * 1000  # outlive the window slightly

    try:
        from redis.exceptions import ResponseError

        client = _get_redis_client()
        args = (now - _ROLLING_WINDOW_SIZE, now, member, ttl_ms)
        try:
            client.eval(_RECORD_FAILURE_LUA, 1, key, *args)
        except ResponseError as exc:
            # Legacy hash-typed key from the pre-M1 implementation.
            if "WRONGTYPE" not in str(exc):
                raise
            client.delete(key)
            client.eval(_RECORD_FAILURE_LUA, 1, key, *args)
    except Exception:
        logger.warning("Redis error recording task failure for %s (ignored)", task_name)


def reset_circuit_breaker(task_name: str | None = None) -> None:
    """Reset circuit breaker state for one or all tasks (#15).

    Called after successful task execution or external intervention.

    M80: with no argument, SCAN over the per-task key prefix and delete each
    match — state only ever lives at ``{base}:{task_name}`` keys, so the old
    ``DELETE`` of the bare base key was a silent no-op and "reset all" never
    reset anything.
    """
    try:
        client = _get_redis_client()
        if task_name is None:
            cursor = 0
            while True:
                cursor, keys = client.scan(
                    cursor=cursor, match=f"{_FAILED_ATTEMPTS_KEY}:*", count=100
                )
                if keys:
                    client.delete(*keys)
                if cursor == 0:
                    break
            logger.info("Reset all circuit breaker state")
        else:
            client.delete(f"{_FAILED_ATTEMPTS_KEY}:{task_name}")
            logger.info("Reset circuit breaker for task %s", task_name)
    except Exception as e:
        logger.warning("Redis error resetting circuit breaker: %s", e)


def toggle_circuit_breaker(enabled: bool) -> None:
    """Enable or disable the circuit breaker entirely (#15)."""
    global _enabled
    _enabled = enabled
    logger.info("Circuit breaker %s", "ENABLED" if enabled else "DISABLED")


# ── Celery application instance ─────────────────────────────────────────────
# Uses the shared ``CircuitBreakerTask`` base so every task enforced by the
# breaker (M6). Defined after the breaker helpers above; decorators, includes
# and monkeypatches all run at import time.
celery_app = Celery(
    "empyrean",
    broker=cfg.REDIS_URL,
    include=[
        "tasks.aggregation",
        "tasks.alerts",
        "tasks.forecast",
        "tasks.process_reading",
    ],
    task_cls=CircuitBreakerTask,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    beat_schedule_filename=_BEAT_SCHEDULE_FILENAME,
    # L61: no result backend is configured and nothing in the repo reads task
    # results (no AsyncResult.get), so drop results instead of storing them.
    task_ignore_result=True,
    # Delivery contract (M96): at-most-once for non-retriable failures.
    # task_acks_late defers the ack until the task finishes, and
    # task_reject_on_worker_lost still redelivers when a worker dies mid-task.
    # But a task that *fails* (or is hard-killed by task_time_limit) is acked
    # and removed for good: the old task_acks_on_failure_or_timeout=False did
    # NOT deliver at-least-once — on_failure rejected with requeue=False,
    # which Kombu's redis transport deletes anyway (silent message loss), and
    # a hard-killed task left unacked was redelivered after visibility_timeout
    # (3600 s) only to be re-killed every hour forever. With the ack on
    # failure/timeout that redelivery loop is gone. Transient DB outages are
    # still covered by the bounded OperationalError autoretry below.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_acks_on_failure_or_timeout=True,
    # Finite time bounds (I-35): configurable via config/__init__.py
    task_soft_time_limit=cfg.TASK_SOFT_TIME_LIMIT,
    task_time_limit=cfg.TASK_HARD_TIME_LIMIT,
    worker_prefetch_multiplier=1,
    # ── Broker resilience (WSL Redis can bounce during startup) ──────────────
    # Retain Celery 5.x startup-retry behaviour in 6.0 (silences the
    # CPendingDeprecationWarning seen on every worker boot).
    broker_connection_retry_on_startup=True,
    # Don't cancel in-flight tasks on a transient broker blip; acks_late +
    # reject_on_worker_lost still redeliver when a worker dies mid-task.
    worker_cancel_long_running_tasks_on_connection_loss=False,
    # L63: startup retries are bounded, not "keep retrying forever": ~30
    # attempts with the backoff below bridge a short broker outage at startup,
    # and once exhausted the process exits so systemd (StartLimitIntervalSec/
    # StartLimitBurst) restarts it. TCP keepalive keeps an idle NAT/WSL
    # connection from being dropped.
    broker_connection_max_retries=30,
    broker_transport_options={
        "max_retries": 30,
        "interval_start": 1,
        "interval_step": 1,
        "interval_max": 5,
        "socket_keepalive": True,
    },
    beat_schedule={
        # ── Every 60 s ──────────────────────────────
        "alert-threshold-check": {
            "task": "empyrean.tasks.alerts.check_thresholds",
            "schedule": 60.0,
        },
        # ── Hourly: aggregation at :07, retraining at :37 ──────────
        # L62: the two heaviest DB jobs used to share crontab(minute=7) and
        # collided every hour; retraining is now offset by half an hour.
        "hourly-aggregation": {
            "task": "empyrean.tasks.aggregation.hourly_aggregate",
            "schedule": crontab(minute=7),
        },
        "forecast-model-retraining": {
            "task": "empyrean.tasks.forecast.retrain_model",
            "schedule": crontab(minute=37),
        },
        # ── Daily at 03:23 ───────────────────────────────
        "data-retention-cleanup": {
            "task": "empyrean.tasks.aggregation.data_retention_cleanup",
            "schedule": crontab(hour=3, minute=23),
        },
        # ── Daily at 03:41 ───────────────────────────────
        # M79: despite its "Runs daily" docstring this task was never
        # scheduled, so expired refresh tokens accumulated forever. Offset
        # from the retention cleanup so the two purges don't run at once.
        "refresh-token-cleanup": {
            "task": "empyrean.tasks.aggregation.refresh_token_cleanup",
            "schedule": crontab(hour=3, minute=41),
        },
    },
)

# Shared task options (M-9 / #15): transient DB/connection blips should recover
# automatically instead of being dropped — with task_acks_on_failure_or_timeout
# (M96) a non-retried failure is acked and gone, so this bounded
# OperationalError autoretry is the only redelivery path for transient infra
# failures. Capped so a flapping DB doesn't storm the queue. Duplicate-PK
# handling in tasks.process_reading still governs idempotent redelivery.
_TASK_AUTORETRY = dict(
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=5,
    retry_jitter=True,
)


# ── Task hooks for automatic failure recording (#15) ─────────────────────────
# Registered globally; record failed attempts to the breaker.
from celery.signals import task_postrun


@task_postrun.connect
def on_task_postrun(sender, task_id, task, retval, state, **kwargs):
    """Record failed attempts for the circuit breaker (M81).

    FAILURE *and* RETRY outcomes both count as failed attempts: with
    autoretry everywhere, a failing task surfaces as RETRY on every attempt
    and only reaches FAILURE once retries are exhausted. The old hook reset
    the window on any non-FAILURE state, so a failing task wiped its own
    failure count on every retry and the breaker could never open.

    Success deliberately does **not** reset the window — the rolling window
    (ZREMRANGEBYSCORE pruning) ages failures out on its own, and a task that
    flaps between success and failure should still trip the breaker. The
    check path (``_circuit_for_task``) only prunes and counts; it never
    records, so checking a circuit cannot feed it.
    """
    if state in ("FAILURE", "RETRY"):
        _record_task_failure(task.name)


# Re-exported for callers who dispatch synchronously and can catch it.
CircuitBreakerOpenError = _OpenCircuitError
