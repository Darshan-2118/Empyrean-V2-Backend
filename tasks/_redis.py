"""Shared lazy sync-Redis client factory for Celery workers.

The sync counterpart to :func:`api.cache.get_client`: one client per worker
process, built on first use and reused for all calls; ``None`` if construction
fails (e.g. a malformed URL) so callers degrade gracefully. ``from_url`` does
not connect — actual connection errors surface on the first command and are
handled per-call.
"""

from __future__ import annotations

import logging

from redis import Redis

from config import get_config

logger = logging.getLogger("empyrean.redis")

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
