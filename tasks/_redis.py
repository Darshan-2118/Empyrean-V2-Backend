"""Shared sync-Redis helpers for Celery workers.

Holds the lazy sync-Redis client factory (the counterpart to
:func:`api.cache.get_client` — one client per worker process, built on first
use and reused for all calls; ``None`` if construction fails so callers degrade
gracefully) and the ``BEAT_HEARTBEAT_KEY`` constant shared between the beat
task that stamps it and the admin health endpoint that reads it. ``from_url``
does not connect — actual connection errors surface on the first command and
are handled per-call.
"""

from __future__ import annotations

import logging
import time

from redis import Redis

from config import get_config

logger = logging.getLogger("empyrean.redis")

# Celery-beat liveness key (shared with the admin health endpoint).
#
# ``tasks.alerts.check_thresholds`` — the most frequent beat task (every 60s) —
# stamps this with the current UTC ISO timestamp on every run; ``GET
# /admin/health`` reads it and reports ``celery_beat`` healthy when the stamp is
# fresh (≤ 3× the schedule interval). Celery beat itself publishes no
# heartbeat, so this task-stamp is the only trustworthy "beat is actually
# firing scheduled work" signal. TTL is 1h so a dead beat leaves the key to
# expire on its own even if nobody polls.
BEAT_HEARTBEAT_KEY = "celery:heartbeat:beat"

_client: Redis | None = None
# Throttle the "could not (re)create client" warning so a long Redis outage
# doesn't spam every task invocation (beat fires every 60s, forecasts on every
# cold request). One warning per this many seconds per process.
_CLIENT_WARN_INTERVAL_S = 60.0
_last_client_warn: float = 0.0


def get_sync_redis() -> Redis | None:
    """Return the process-wide sync Redis client, creating it lazily.

    Self-healing: if the initial construction failed (Redis briefly
    unreachable at worker startup) we try again on each call instead of
    permanently returning ``None`` for the worker's lifetime (#14). A transient
    startup blip would otherwise permanently disable the beat heartbeat,
    forecast cache, and latest-reading write-through until the process restarted.
    """
    global _client, _last_client_warn
    if _client is not None:
        return _client
    try:
        _client = Redis.from_url(get_config().REDIS_URL, decode_responses=True)
        logger.info("Redis client created for %s", get_config().REDIS_URL)
    except Exception:
        now = time.monotonic()
        if now - _last_client_warn >= _CLIENT_WARN_INTERVAL_S:
            logger.warning(
                "Failed to (re)create Redis client — Redis-backed features "
                "disabled until it becomes reachable: %s",
                get_config().REDIS_URL,
            )
            _last_client_warn = now
        _client = None
    return _client
