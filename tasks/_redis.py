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


def get_sync_redis() -> Redis | None:
    """Return the process-wide sync Redis client, creating it lazily."""
    global _client
    if _client is None:
        try:
            _client = Redis.from_url(get_config().REDIS_URL, decode_responses=True)
        except Exception:
            logger.exception("Failed to create Redis client — cache disabled")
            _client = None
    return _client
