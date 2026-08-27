"""
Redis read-through cache helpers (API side).

A single async Redis client is created lazily and reused. Every operation
**degrades gracefully**: if Redis is unreachable we log and return ``None`` /
no-op, so the API keeps serving from the DB instead of 500ing.

Redis keys used by this module (contract from ``docs/database.md``):

=============== ===== =================================================
Key             TTL   Value
=============== ===== =================================================
``readings:latest``       60s  JSON array of ``LatestReading`` objects
``readings:latest:{node_id}``  60s  Latest enriched reading for one node
``nodes:all``             300s  JSON array of ``NodeResponse`` objects
``ratelimit:{endpoint}:{ip}:{minute}``  60s  Request count (int) — see api/rate_limit.py
``celery:forecast:{node_id}`` 3600s  AQI forecast JSON array — tasks side
=============== ===== =================================================

``ratelimit:*`` keys are owned by :mod:`api.rate_limit` and
``celery:forecast:*`` by the Celery worker; this module only defines the
generic get/set helpers they all share.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from redis.asyncio import Redis

from config import get_config

logger = logging.getLogger("empyrean.cache")

_client: Redis | None = None

# Throttle the per-call "cache disabled" warning so a cache that was never
# built doesn't spam every request (see #16). One warning per this many seconds.
_CACHE_WARN_INTERVAL_S = 60.0
_last_cache_warn: float = 0.0
# M18: separate throttle for construction-failure warnings (mirrors
# tasks/_redis.py), so a long outage doesn't log on every get_client() call.
_last_client_warn: float = 0.0


def get_client() -> Redis | None:
    """Return the shared async Redis client, creating it lazily.

    Returns ``None`` if the client could not be constructed (e.g. a malformed
    URL) so callers can degrade. Note: ``from_url`` does not connect — actual
    connection errors surface on the first command and are handled per-call.

    Self-healing (M18, mirrors ``tasks/_redis.py``): a failed construction
    leaves ``_client`` at ``None`` so the next call retries — a worker that
    boots during a Redis outage recovers without a restart.
    """
    global _client, _last_client_warn
    if _client is not None:
        return _client
    try:
        # L15: resolve config per call — a module-level ``cfg`` snapshot went
        # stale after reset_config_cache() in tests.
        redis_url = get_config().REDIS_URL
        _client = Redis.from_url(redis_url, decode_responses=True)
        logger.info("Redis cache client created for %s", redis_url)
    except Exception:
        now = time.monotonic()
        if now - _last_client_warn >= _CACHE_WARN_INTERVAL_S:
            logger.warning(
                "Failed to (re)create Redis cache client — cache disabled "
                "until Redis becomes reachable"
            )
            _last_client_warn = now
        _client = None
    return _client


def reset_cache_client() -> None:
    """Drop the process-global client so the next ``get_client()`` rebuilds it.

    M22: ``reset_config_cache()`` alone left ``_client`` pointing at the old
    REDIS_URL; tests that mock ``Redis.from_url`` or repoint the URL need this
    hook (wired into ``tests/conftest.py``).
    """
    global _client, _last_client_warn
    _client = None
    _last_client_warn = 0.0


def _warn_cache_disabled() -> None:
    """Emit a throttled WARNING that the cache layer is currently unavailable.

    The per-call ``client is None`` guards in the helper functions below used to
    be silent, so a never-built cache looked identical to a cache miss and
    masqueraded as normal DB-hot traffic (#16). Throttled so it doesn't log on
    every request.
    """
    global _last_cache_warn
    now = time.monotonic()
    if now - _last_cache_warn >= _CACHE_WARN_INTERVAL_S:
        logger.warning(
            "Redis cache client unavailable — cache layer disabled; reads fall "
            "through to PostgreSQL (set REDIS_URL and start Redis)"
        )
        _last_cache_warn = now


async def cache_get(key: str) -> str | None:
    """Return the raw string value for ``key``, or ``None`` on miss / Redis down."""
    client = get_client()
    if client is None:
        _warn_cache_disabled()
        return None
    try:
        return await client.get(key)
    except Exception:
        logger.warning("Redis GET failed for %r — serving from DB", key)
        return None


async def cache_set(key: str, value: str, ttl: int) -> None:
    """Set ``key`` to ``value`` with a ``ttl`` in seconds (no-op if Redis down)."""
    client = get_client()
    if client is None:
        _warn_cache_disabled()
        return
    try:
        await client.setex(key, ttl, value)
    except Exception:
        logger.warning("Redis SETEX failed for %r — skipping cache write", key)


async def cache_get_json(key: str) -> dict | list | None:
    """Return a JSON-decoded cached value, or ``None`` on miss / Redis down."""
    raw = await cache_get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Cached value for %r is not valid JSON", key)
        return None


async def cache_set_json(key: str, obj: Any, ttl: int) -> None:
    """JSON-encode ``obj`` and cache it under ``key`` for ``ttl`` seconds."""
    try:
        payload = json.dumps(obj)
    except (TypeError, ValueError):
        logger.warning("Cannot JSON-serialize value for %r — skipping write", key)
        return
    await cache_set(key, payload, ttl)


async def cache_delete(key: str) -> None:
    """Delete ``key`` from Redis (best-effort; no-op if Redis is down)."""
    client = get_client()
    if client is None:
        _warn_cache_disabled()
        return
    try:
        await client.delete(key)
    except Exception:
        logger.warning("Redis DEL failed for %r — skipping invalidation", key)
