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
rather than blocking the API, and the response carries the usual
``X-RateLimit-*`` headers **plus** ``X-RateLimit-Bypass: true`` (M19) so the
numbers are never mistaken for an active limit. When failing open, a
WARNING is logged and a counter is incremented so `/admin/health` can report
rate-limit bypass state (#03).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from functools import wraps
from threading import Lock
from typing import Any, Callable

from quart import request

from api.cache import get_client
from api.jwt import problem_json

logger = logging.getLogger("empyrean.ratelimit")

DEFAULT_LIMIT = 200
DEFAULT_WINDOW_SECONDS = 60

# M17: there is deliberately NO in-memory fallback limiter. The old
# ``_in_memory_cache`` dict was never populated, so a Redis outage silently
# meant no rate limit while the docstrings claimed a working per-process
# fallback. The dead dict is removed; the limiter now fails open honestly and
# reports the degraded state via the bypass counter / /admin/health.

# Track rate-limit bypass events (#03 — observability)
# Incremented when Redis unavailable or errors; checked by /admin/health
_bypass_events = 0
_bypass_events_lock = Lock()


def is_rate_limit_available() -> bool:
    """True when the distributed (Redis-backed) limiter is usable.

    False means the decorator is failing open with **no** rate limiting
    (M17 — the old in-memory fallback was dead and is removed), so requests
    are unthrottled. Used by ``GET /admin/health`` to surface rate-limit state.
    """
    return get_client() is not None


def get_rate_limit_bypass_count() -> int:
    """Return the count of rate-limit bypass events since app startup.

    Used by /admin/health (#03) to surface when Redis has been unavailable,
    putting the limiter into its fail-open state (M17). A non-zero count
    indicates rate limiting was off for a period.
    """
    with _bypass_events_lock:
        return _bypass_events


def _increment_bypass_counter() -> None:
    """Increment the rate-limit bypass counter (#03).

    Called when Redis is unavailable or errored. Since M17 removed the dead
    in-memory fallback, a bypass means rate limiting is genuinely off for this
    process — the counter gives /admin/health observability into that.
    """
    global _bypass_events
    with _bypass_events_lock:
        _bypass_events += 1


def _client_ip() -> str:
    """Client IP for rate limiting (H12/H31).

    By default only ``request.remote_addr`` is used: a client-supplied
    ``X-Forwarded-For`` header is never trusted, since honoring its first entry
    lets an attacker mint a fresh bucket per request and poison a victim's
    bucket.

    When ``TRUST_PROXY_HEADERS`` is enabled in config, the ``X-Real-IP`` header
    set by the *trusted* reverse proxy (deploy/nginx.conf) is honored instead —
    otherwise every proxied request shares the proxy's address (e.g.
    ``127.0.0.1``) and one user can exhaust the single shared bucket for
    everyone. Only enable this when the API is not directly reachable.
    """
    if _trust_proxy():
        real_ip = request.headers.get("X-Real-IP", "").strip()
        if real_ip:
            return real_ip
    return request.remote_addr or "unknown"


def _trust_proxy() -> bool:
    """Read the TRUST_PROXY_HEADERS flag lazily so tests can repoint config."""
    from config import get_config

    return get_config().TRUST_PROXY_HEADERS


def _rate_limit_key(ip: str, now: datetime) -> str:
    """Build the per-endpoint, per-IP, per-minute Redis key for ``now``.

    Scoping by ``request.endpoint`` (e.g. ``auth.login``, ``readings.latest``)
    keeps each route's budget independent: without it every endpoint would share
    one bucket per IP, so one hot route (or one heavy client) could burn the
    whole budget and deny all other endpoints on that IP. ``request.endpoint``
    is populated for any request that matched a route; a literal ``default``
    scope keeps the key stable if it is ever missing.

    Granularity (M16): the bucket is keyed on the **view-function name**, NOT
    the URL path. A single handler that serves many sub-paths (e.g. a
    ``/<node_id>`` route) therefore shares one bucket across all of those
    paths for a given IP. This is intentional — a per-path bucket would let a
    client mint unbounded buckets. If per-path isolation is ever needed, add a
    separate, bounded path key rather than widening this one.
    """
    endpoint = request.endpoint or "default"
    minute = now.strftime("%Y%m%d%H%M")
    return f"ratelimit:{endpoint}:{ip}:{minute}"


def _reset_epoch(now: datetime) -> int:
    """Unix epoch seconds when the current UTC minute window resets."""
    return int(now.timestamp()) + (60 - now.second)


def _rate_headers(
    limit: int, remaining: int, reset_ts: int, bypass: bool = False
) -> dict[str, str]:
    """Build the ``X-RateLimit-*`` response headers.

    M19: fail-open responses carry ``X-RateLimit-Bypass: true`` so the
    limit/remaining values are never mistaken for an active limit — without
    the label, a bypassed response looked identical to an unconsumed bucket.
    """
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(remaining, 0)),
        "X-RateLimit-Reset": str(reset_ts),
    }
    if bypass:
        headers["X-RateLimit-Bypass"] = "true"
    return headers


def _with_headers(
    result: Any, limit: int, remaining: int, reset_ts: int, bypass: bool = False
) -> Any:
    """Attach rate-limit headers to a Quart response or ``(body, status[, headers])`` tuple."""
    headers = _rate_headers(limit, remaining, reset_ts, bypass)
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

    On breach returns an RFC 7807 ``429 Too Many Requests``. When Redis is
    unavailable the limiter **fails open** (M17): there is deliberately no
    in-memory fallback dict (the old one was dead, never populated), so a
    Redis outage means no shared rate limit rather than a silently ineffective
    one. ``/admin/health`` reports this degraded state via the bypass counter.
    Attaches ``X-RateLimit-*`` headers to every response.
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
                            problem_json(
                                429, "Too Many Requests", "Rate limit exceeded. Please slow down."
                            ),
                            limit,
                            limit - count,
                            reset_ts,
                        )
                    return _with_headers(await f(*args, **kwargs), limit, limit - count, reset_ts)
                # Redis INCR errored -> fail open (M17): no unreliable fallback.
                logger.warning("Redis rate-limit INCR failed for %r — failing open", key)
                _increment_bypass_counter()
            else:
                # Redis client unavailable -> fail open (M17).
                _increment_bypass_counter()

            # Fail open: allow request through with headers, labelled as a
            # bypass (M19) so clients can tell no limit was enforced.
            return _with_headers(await f(*args, **kwargs), limit, limit, reset_ts, bypass=True)

        return decorated

    return decorator
