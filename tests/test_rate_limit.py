"""Rate-limit IP extraction tests (H-5).

The client IP must come from ``request.remote_addr`` only — never the first
client-supplied ``X-Forwarded-For`` entry, which would allow an attacker to mint
a fresh bucket per request (bypass) and burn a victim's bucket (DoS).

Quart derives ``remote_addr`` from the ``Remote-Addr`` header set by the trusted
ASGI transport/proxy layer; the test simulates that trusted value and a spoofed
XFF header side-by-side.

The Redis key itself is also scoped per endpoint (``ratelimit:{endpoint}:{ip}:
{minute}``), so one route cannot exhaust the whole per-IP allowance — that
key-builder contract is asserted here too.
"""

import asyncio
from datetime import datetime, timezone

from quart import Quart, request

from app import create_app
from api.rate_limit import _client_ip, _incr, _rate_limit_key

app = Quart(__name__)


def _run(ip: str | None, xff: str | None) -> str:
    headers = {}
    if ip is not None:
        headers["Remote-Addr"] = ip
    if xff is not None:
        headers["X-Forwarded-For"] = xff

    async def check():
        async with app.test_request_context("/", headers=headers):
            return request.remote_addr, _client_ip()

    remote, bucket_ip = asyncio.run(check())
    assert bucket_ip == (ip or "unknown")
    assert bucket_ip == (remote or "unknown")
    return bucket_ip


def test_remote_addr_is_authoritative_over_spoofed_xff():
    """A spoofed XFF first entry must never become the rate-limit key."""
    ip = _run("192.0.2.10", "203.0.113.9, 10.0.0.1")
    assert ip == "192.0.2.10"
    assert ip != "203.0.113.9"


def test_single_xff_ignored():
    """Even a lone XFF header is not trusted."""
    ip = _run("198.51.100.7", "203.0.113.66")
    assert ip == "198.51.100.7"


def test_missing_remote_addr_falls_back_to_unknown():
    """No trusted address -> 'unknown' bucket (never the spoofed XFF)."""
    ip = _run(None, "203.0.113.9")
    assert ip == "unknown"


# ── L-31 · INCR + PEXPIRE is atomic via a single Lua eval ──────────────────────


def test_incr_uses_atomic_lua_script():
    """L-31: _incr runs INCR + PEXPIRE as one eval, not two round-trips.

    A process death between separate INCR/EXPIRE calls could leave a rate-limit
    key with no TTL; the Lua script does both atomically. We assert the call
    pattern against a recording fake async client.
    """
    class _FakeClient:
        def __init__(self):
            self.eval_calls = []

        async def eval(self, script, numkeys, key, ttl_ms):
            self.eval_calls.append((script, numkeys, key, ttl_ms))
            return 1

    async def scenario():
        client = _FakeClient()
        count = await _incr(client, "ratelimit:1.2.3.4:202608050101", 60)
        assert count == 1
        assert len(client.eval_calls) == 1
        script, numkeys, key, ttl_ms = client.eval_calls[0]
        assert numkeys == 1
        assert "INCR" in script and "PEXPIRE" in script
        assert key == "ratelimit:1.2.3.4:202608050101"
        assert ttl_ms == 60_000  # window_seconds * 1000, passed to PEXPIRE

    asyncio.run(scenario())


def test_incr_fails_open_when_eval_raises():
    """L-31: a Redis eval error makes _incr return None so the caller fails open."""
    class _RaisingClient:
        async def eval(self, *args, **kwargs):
            raise ConnectionError("redis down")

    async def scenario():
        result = await _incr(_RaisingClient(), "ratelimit:1.2.3.4:202608050101", 60)
        assert result is None

    asyncio.run(scenario())


# ── H-5 · Redis key is scoped per endpoint, not just per IP+minute ─────────────


def test_rate_limit_key_includes_endpoint_scope():
    """H-5: the key is ``ratelimit:{endpoint}:{ip}:{minute}``.

    Scoping by the matched Quart route keeps each endpoint's bucket
    independent, so one hot route cannot exhaust the whole per-IP allowance.
    ``request.endpoint`` is ``auth.login`` for POST /api/v1/auth/login; a
    request that matches no route falls back to a stable ``default`` scope.
    """
    minute = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

    async def check():
        app = create_app()
        # A real matched route → endpoint-scoped key.
        async with app.test_request_context("/api/v1/auth/login", method="POST"):
            key = _rate_limit_key("192.0.2.10", minute)
            assert key == "ratelimit:auth.login:192.0.2.10:202608061200"

        # Unmatched path → stable "default" scope (no crash on missing endpoint).
        async with app.test_request_context("/no-such-route"):
            key = _rate_limit_key("192.0.2.10", minute)
            assert key == "ratelimit:default:192.0.2.10:202608061200"

    asyncio.run(check())
