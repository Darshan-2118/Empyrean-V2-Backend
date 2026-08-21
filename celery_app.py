"""
Celery application instance for async task processing.

Broker:    Redis (configured via REDIS_URL)
Backend:   Redis (same URL)
Beat:      Scheduled tasks defined below

Task modules under ``tasks/`` are imported eagerly via ``include`` so their
``@celery_app.task``-decorated callables register with the worker.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from celery import Celery
from celery.schedules import crontab
from sqlalchemy.exc import OperationalError

from config import get_config

cfg = get_config()

logger = logging.getLogger(__name__)

# ── Celery application instance ─────────────────────────────────────────────
# Must be defined before anything below that references it (decorators,
# monkeypatches, etc. all execute at import time).
celery_app = Celery(
    "empyrean",
    broker=cfg.REDIS_URL,
    backend=cfg.REDIS_URL,
    include=[
        "tasks.aggregation",
        "tasks.alerts",
        "tasks.forecast",
        "tasks.process_reading",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    # At-least-once delivery (M-6): ack only after the task finishes
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_acks_on_failure_or_timeout=False,
    # Finite time bounds (I-35): configurable via config/__init__.py
    task_soft_time_limit=cfg.TASK_SOFT_TIME_LIMIT,
    task_time_limit=cfg.TASK_HARD_TIME_LIMIT,
    worker_prefetch_multiplier=1,
    beat_schedule={
        # ── Every 60 s ──────────────────────────────
        "alert-threshold-check": {
            "task": "empyrean.tasks.alerts.check_thresholds",
            "schedule": 60.0,
        },
        # ── Every hour (7 minutes past) ────────────────────────────
        "hourly-aggregation": {
            "task": "empyrean.tasks.aggregation.hourly_aggregate",
            "schedule": crontab(minute=7),
        },
        "forecast-model-retraining": {
            "task": "tasks.forecast.retrain_model",
            "schedule": crontab(minute=7),
        },
        # ── Daily at 03:23 ───────────────────────────────
        "data-retention-cleanup": {
            "task": "empyrean.tasks.aggregation.data_retention_cleanup",
            "schedule": crontab(hour=3, minute=23),
        },
    },
)

# Shared task options (M-9 / #15): transient DB/connection blips should recover
# automatically instead of being dropped (acks_late means a hard failure is
# redelivered with no retry bound → permanent loss). All real tasks retry on
# OperationalError with exponential backoff, capped so a flapping DB doesn't
# storm the queue. Duplicate-PK handling in tasks.process_reading still governs
# idempotent redelivery; this only bounds transient infra failures.
_TASK_AUTORETRY = dict(
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=5,
    retry_jitter=True,
)

# ── Redis-backed circuit breaker (#15) ─────────────────────────────────────
# Tracks failed task attempts perRetryKey to implement a circuit breaker that
# prevents retry storms. When a task fails N times in a rolling window, subsequent
# attempts skip retries and are logged as "circuit open".
_FAILED_ATTEMPTS_KEY = "celery:circuit_breaker:failed_attempts"
_MAX_FAILED_ATTEMPTS_WINDOW = 300  # 5 minutes
_MAX_FAILED_ATTEMPTS = 10  # After 10 failures, circuit opens
_ROLLING_WINDOW_SIZE = 60  # Count failures in 60s windows (5 windows)

_lock = threading.Lock()
_enabled = True


def _circuit_for_task(task_name: str) -> bool:
    """Check if retries should be allowed for *task_name*.

    Returns False if the task has exceeded its failure tolerance in the last
    MAX_FAILED_ATTEMPTS_WINDOW, indicating a circuit is open. Returns True
    otherwise. This is a Redis-backed circuit breaker with rolling fixed
    windows (#15).
    """
    if not _enabled:
        return True

    now = int(datetime.now(timezone.utc).timestamp())
    key = f"{_FAILED_ATTEMPTS_KEY}:{task_name}"

    from redis import Redis
    import os

    redis_url = os.getenv("REDIS_URL", cfg.REDIS_URL)
    try:
        client = Redis.from_url(redis_url, decode_responses=False)
    except Exception:
        return True  # Gracefully degrade if Redis is temporarily unavailable

    try:
        # Get the current timestamp
        current_ts = client.hget(key, "ts")
        if not current_ts:
            # First attempt - initialize counter
            client.hset(key, {"ts": str(now), "count": "0"})
            return True

        current_ts = int(current_ts)
        count_str = client.hget(key, "count")
        count = int(count_str) if count_str else 0

        # Remove timestamps older than the rolling window
        cutoff = now - _MAX_FAILED_ATTEMPTS_WINDOW
        if current_ts < cutoff:
            client.hdel(key, "ts")
            client.hset(key, {"ts": str(now), "count": "0"})
            return True

        # If we've exceeded the failure threshold, block further retries
        if count >= _MAX_FAILED_ATTEMPTS:
            logger.warning(
                "Circuit open for task '%s' - %d failures in %.1f-minute window, "
                "skipping retries",
                task_name,
                count,
                _MAX_FAILED_ATTEMPTS_WINDOW / 60.0,
            )
            return False

        # Increment counter for this attempt
        client.hincrby(key, "count", 1)
        return True

    except Exception:
        logger.exception("Redis error checking circuit breaker for task %s", task_name)
        return True  # Fail open on Redis errors
    finally:
        try:
            client.close()
        except Exception:
            pass


def _record_task_failure(task_name: str) -> None:
    """Record a failed task execution for circuit breaker tracking (#15).

    Called automatically by all tasks that use autoretry_for.
    """
    if not _enabled:
        return

    now = int(datetime.now(timezone.utc).timestamp())
    key = f"{_FAILED_ATTEMPTS_KEY}:{task_name}"

    from redis import Redis
    import os

    redis_url = os.getenv("REDIS_URL", cfg.REDIS_URL)
    try:
        client = Redis.from_url(redis_url, decode_responses=False)
    except Exception:
        return

    try:
        # Increment counter for this failure
        client.hincrby(key, "count", 1)
        client.hset(key, {"ts": str(now)})

        # TTL expires the window automatically (3600s)
        client.expire(key, 3600)
    except Exception:
        logger.exception("Redis error recording task failure for %s", task_name)
    finally:
        try:
            client.close()
        except Exception:
            pass


def reset_circuit_breaker(task_name: str | None = None) -> None:
    """Reset circuit breaker state for one or all tasks (#15).

    Called after successful task execution or external intervention.
    """
    if task_name is None:
        from redis import Redis
        import os

        redis_url = os.getenv("REDIS_URL", cfg.REDIS_URL)
        try:
            client = Redis.from_url(redis_url, decode_responses=False)
            client.delete(_FAILED_ATTEMPTS_KEY)
            logger.info("Reset all circuit breaker state")
        except Exception as e:
            logger.exception("Redis error resetting circuit breaker: %s", e)
        finally:
            try:
                client.close()
            except Exception:
                pass
    else:
        from redis import Redis
        import os

        redis_url = os.getenv("REDIS_URL", cfg.REDIS_URL)
        try:
            client = Redis.from_url(redis_url, decode_responses=False)
            client.delete(f"{_FAILED_ATTEMPTS_KEY}:{task_name}")
            logger.info("Reset circuit breaker for task %s", task_name)
        except Exception as e:
            logger.exception("Redis error resetting circuit breaker %s: %s", task_name, e)
        finally:
            try:
                client.close()
            except Exception:
                pass


def toggle_circuit_breaker(enabled: bool) -> None:
    """Enable or disable the circuit breaker entirely (#15)."""
    global _enabled
    _enabled = enabled
    logger.info("Circuit breaker %s", "ENABLED" if enabled else "DISABLED")


# ── Task hooks for automatic failure recording (#15) ─────────────────────────
# These hooks are registered globally and automatically record task failures
# to the circuit breaker when autoretry_for is in use.

from celery.signals import task_prerun, task_postrun


@task_prerun.connect
def on_task_prerun(sender, task_id, task, **kwargs):
    """Called before every task starts. No failure recording here to avoid double-counting."""
    # Failure recording moved to task_postrun to count only actual failures
    pass


@task_postrun.connect
def on_task_postrun(sender, task_id, task, retval, state, **kwargs):
    """Called after every task completes. Record failures and reset on success."""
    if hasattr(task, "autoretry_for") and task.autoretry_for:
        if state == 'FAILURE':
            # Record failure for circuit breaker
            _record_task_failure(task.name)
        else:
            # Task succeeded (or retried/reset): clear failure count for this task
            reset_circuit_breaker(task.name)


# ── Task middleware to integrate circuit breaker (#15) ───────────────────────
# Wraps task execution to check circuit breaker state before retrying.

original_task_apply_async = celery_app.send_task


def circuit_breaker_aware_send_task(name: str, task_dict=None, *args, **kwargs):
    """Send task with circuit breaker awareness (#15).

    Logs retry attempts and checks circuit breaker state before execution.
    """
    if _circuit_for_task(name):
        return original_task_apply_async(name, task_dict, *args, **kwargs)
    else:
        logger.error(
            "Task '%s' skipped due to circuit being OPEN (10 failures in 5 min window). "
            "Trigger reset via: python -c 'from celery_app import reset_circuit_breaker; "
            "reset_circuit_breaker(\"%s\")'",
            name,
            name,
        )
        raise RuntimeError(f"Circuit breaker open for task {name}")


celery_app.send_task = circuit_breaker_aware_send_task