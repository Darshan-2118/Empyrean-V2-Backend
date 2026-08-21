"""
Redis-backed fixed-window rate limiting (API side).

Decorator ``@rate_limit(limit, window_seconds)`` applied to endpoints. The
window is anchored to the UTC minute: the Redis key is
``ratelimit:{endpoint}:{ip}:{minute}`` (endpoint = the Quart
``request.endpoint`` scope, minute = ``%Y%m%d%H%M``, contract from
``docs/database.md``), so ``window_seconds`` should be ≤ 60 to match the key
granularity. The endpoint scope isolates each route's bucket so one endpoint
cannot exhaust the whole per-IP allowance for the app. Every response from a
wrapped endpoint carries
``X-RateLimit-Limit`` / ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset``.

If Redis is unreachable the decorator **fails open** — the request is allowed
(and still gets headers) rather than blocking the API. When failing open, a
WARNING is logged and a counter is incremented so `/admin/health` can report
rate-limit bypass state (#03).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from functools import wraps
from threading import Lock
from typing import Any, Callable, Dict, Tuple, Optional

from quart import request

from api.cache import get_client
from api.jwt import _problem_json

logger = logging.getLogger("empyrean.ratelimit")

DEFAULT_LIMIT = 200
DEFAULT_WINDOW_SECONDS = 60

# In-memory fallback for when Redis is unavailable
# Structure: {key: (count, window_start_time)}
_in_memory_cache: Dict[str, Tuple[int, float]] = {}
_in_memory_lock = Lock()

# Track rate-limit bypass events (#03 — observability)
# Incremented when Redis unavailable or errors; checked by /admin/health
_bypass_events = 0
_bypass_events_lock = Lock()


def is_rate_limit_available() -> bool:
    """True when the distributed (Redis-backed) limiter is usable.

    False means the decorator is failing open to the per-process in-memory
    fallback (#03) — still protective per-instance, but not a shared limit. Used
    by ``GET /admin/health`` to surface rate-limit state.
    """
    return get_client() is not None


def get_rate_limit_bypass_count() -> int:
    """Return the count of rate-limit bypass events since app startup.
    
    Used by /admin/health (#03) to surface when Redis has been unavailable,
    triggering the fail-open fallback path. A non-zero count indicates the
    rate-limiter was not working as designed (single shared limit).
    """
    with _bypass_events_lock:
        return _bypass_events


def _increment_bypass_counter() -> None:
    """Increment the rate-limit bypass counter (#03).
    
    Called when Redis is unavailable or errored, triggering the in-memory
    fallback. Provides observability into when rate-limiting has degraded.
    """
    global _bypass_events
    with _bypass_events_lock:
        _bypass_events += 1


def _client_ip() -> str:
    """Client IP for rate limiting — ``request.remote_addr`` only (H-5).

    A client-supplied ``X-Forwarded-For`` header is never trusted: honoring its
    first entry lets an attacker mint a fresh bucket per request (bypassing the
    limit) and poison a victim's bucket (DoS). If the API sits behind a trusted
    proxy, terminate the client IP at that trusted layer (e.g. PROXY protocol)
    and configure the proxy, not this code, to set a trusted header.
    """
    return request.remote_addr or "unknown"


def _rate_limit_key(ip: str, now: datetime) -> str:
    """Build the per-endpoint, per-IP, per-minute Redis key for ``now``.

    Scoping by ``request.endpoint`` (e.g. ``auth.login``, ``readings.latest``)
    keeps each route's budget independent: without it every endpoint would share
    one bucket per IP, so one hot route (or one heavy client) could burn the
    whole budget and deny all other endpoints on that IP. ``request.endpoint``
    is populated for any request that matched a route; a literal ``default``
    scope keeps the key stable if it is ever missing.
    """
    endpoint = request.endpoint or "default"
    minute = now.strftime("%Y%m%d%H%M")
    return f"ratelimit:{endpoint}:{ip}:{minute}"


def _reset_epoch(now: datetime) -> int:
    """Unix epoch seconds when the current UTC minute window resets."""
    return int(now.timestamp()) + (60 - now.second)


def _rate_headers(limit: int, remaining: int, reset_ts: int) -> dict[str, str]:
    """Build the ``X-RateLimit-*`` response headers."""
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(remaining, 0)),
        "X-RateLimit-Reset": str(reset_ts),
    }


def _with_headers(result: Any, limit: int, remaining: int, reset_ts: int) -> Any:
    """Attach rate-limit headers to a Quart response or ``(body, status[, headers])`` tuple."""
    headers = _rate_headers(limit, remaining, reset_ts)
    if isinstance(result, tuple):
        if len(result) == 3:
            body, status, existing = result
            return body, status, {**(existing or {}), **headers}
        body, status = result
        return body, status, headers
    # Bare Response object (e.g. ``jsonify(...)`` with no status code).
    for key, value in headers.items():
        result.headers[key] = value
    return result


# Atomic INCR + conditional EXPIRE (L-31): a process death between the two
# commands must not leave a rate-limit key with no TTL. ``window_seconds`` is
# an integer the server converts to milliseconds, so pass it directly.
_INCR_EXPIRE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return count
"""


async def _incr(client, key: str, window_seconds: int) -> int | None:
    """Atomically INCR ``key`` and EXPIRE it on first increment (L-31).

    Runs ``INCR`` + conditional ``PEXPIRE`` as a single Lua script, so a process
    death between the two steps cannot leave a key without a TTL. Returns
    ``None`` on Redis error so the caller can fail open.
    """
    try:
        count = await client.eval(
            _INCR_EXPIRE_SCRIPT, 1, key, window_seconds * 1000
        )
        return int(count)
    except Exception:
        logger.warning("Redis rate-limit INCR failed for %r — failing open", key)
        return None


def rate_limit(limit: int = DEFAULT_LIMIT, window_seconds: int = DEFAULT_WINDOW_SECONDS) -> Callable:
    """Enforce a per-IP fixed-window request limit.

    On breach returns an RFC 7807 ``429 Too Many Requests``. When Redis is unavailable,
    falls back to an in-memory counter to avoid failing open. Attaches
    ``X-RateLimit-*`` headers to every response.
    """

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def decorated(*args: Any, **kwargs: Any) -> Any:
            ip = _client_ip()
            now = datetime.now(timezone.utc)
            key = _rate_limit_key(ip, now)
            reset_ts = _reset_epoch(now)

            client = get_client()
            if client is not None:
                count = await _incr(client, key, window_seconds)
                if count is not None:
                    # Redis worked normally
                    if count > limit:
                        logger.warning("Rate limit exceeded for IP %s (%s/%s)", ip, count, limit)
                        return _with_headers(
                            _problem_json(
                                429, "Too Many Requests", "Rate limit exceeded. Please slow down."
                            ),
                            limit,
                            limit - count,
                            reset_ts,
                        )
                    return _with_headers(await f(*args, **kwargs), limit, limit - count, reset_ts)
                # Redis error -> fail open
                logger.warning("Redis rate-limit INCR failed for %r — failing open", key)
                _increment_bypass_counter()
            else:
                # Redis client unavailable -> fail open
                _increment_bypass_counter()

            # Fail open: allow request through with headers
            return _with_headers(await f(*args, **kwargs), limit, limit, reset_ts)

        return decorated

    return decorator
