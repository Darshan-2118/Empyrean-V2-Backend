"""
Behavioral API tests against the ``empyrean_test`` DB (M-16).

These exercise the real Quart app over HTTP (``app.test_client()``) with real
DB writes, covering the register/login/refresh rotation, logout, profile CRUD,
change-password, readings latest/history, forecast, rate-limit headers, and the
RFC 7807 error contract — the endpoint behaviour ``test_routes.py`` (URL map
only) cannot.

Environment notes:

* **Redis is unreachable in this environment**, and every real connect attempt
  blocks for the OS timeout (~2-4 s). The app is designed to *fail open* when
  Redis is down, so an autouse fixture patches the cache/rate-limit client to
  ``None`` — the exact documented degrade path — keeping the suite fast and
  deterministic while still exercising the fail-open branch (rate-limit headers
  are still attached, cache reads return ``None``). The 429 breach path is
  tested separately by stubbing ``_incr``.
* **TimescaleDB's ``time_bucket``** is used by ``/readings/history``. conftest
  creates tables via ``metadata.create_all`` (no migrations), so the extension
  is created on demand by the ``timescale_available`` fixture; the history test
  is skipped if the server cannot provide it.
* **Event loops:** tests are plain sync functions that run their scenario with
  ``asyncio.run()``. The app's engines are module-level singletons (models/base),
  so each scenario disposes them before ``asyncio.run`` closes its loop — this
  avoids the Windows Proactor + asyncpg cross-loop pool errors that
  pytest-asyncio's per-test loop management triggers.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from quart import Quart, jsonify
from sqlalchemy import delete, select

from api.jwt import (
    admin_required,
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    jwt_required,
)
from app import create_app
from models import Node, RefreshToken, SensorReading, User
from models.base import AsyncSessionLocal, dispose_engines
from models.helpers import hash_password

API = "/api/v1"

# Rows this module commits (register/_create_user/nodes/readings) so they can be
# removed at the end of each scenario. conftest's tables are session-scoped
# (dropped only at session end), so without this cleanup committed rows would
# leak into later test modules (e.g. test_smoke's row-count check or the alert
# sweep) that assume a clean DB.
_CREATED_USERNAMES: set[str] = set()
_CREATED_NODE_IDS: set[str] = set()


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_redis_down(monkeypatch):
    """Simulate an unreachable Redis *fast* (documented fail-open path).

    Every endpoint either rate-limits or read-through-caches via Redis. With no
    Redis server the ops would block for the OS connect timeout; patching the
    cache client to ``None`` makes them fast no-ops without changing behaviour
    (see api/cache.py and api/rate_limit.py docs).
    """
    monkeypatch.setattr("api.rate_limit.get_client", lambda: None)
    monkeypatch.setattr("api.cache.get_client", lambda: None)
    monkeypatch.setattr("tasks.forecast._redis", lambda: None)


@pytest.fixture(scope="session")
def timescale_available() -> bool:
    """Create the timescaledb extension in the test DB; return availability."""
    try:
        import os

        import psycopg2

        from config import get_config
        from sqlalchemy.engine import make_url

        # Target the DB this session actually runs against (TEST_DATABASE_URL
        # when the suite is pointed at an isolated DB) — NOT a hardcoded name —
        # otherwise the extension check and the queries under test can disagree.
        url = make_url(os.environ.get("TEST_DATABASE_URL") or get_config().DATABASE_URL)
        conn = psycopg2.connect(url.render_as_string(hide_password=False))
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


# ── Helpers ────────────────────────────────────────────────────────────────────


def _unique(prefix: str) -> str:
    """Return a unique slug for test-scoped rows (avoid cross-test collisions).

    Usernames must match ``^[A-Za-z0-9_]+$`` (api/schemas.py), so the slug uses
    an underscore, not a hyphen.
    """
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _iso_utc(dt: datetime) -> str:
    """ISO-8601 UTC string with ``Z`` suffix (safe in a query string)."""
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cleanup_tracked_rows() -> None:
    """Delete every user/node this module committed (FK cascades do the rest)."""
    async with AsyncSessionLocal() as session:
        if _CREATED_USERNAMES:
            await session.execute(
                delete(User).where(User.username.in_(_CREATED_USERNAMES))
            )
        if _CREATED_NODE_IDS:
            await session.execute(
                delete(Node).where(Node.node_id.in_(_CREATED_NODE_IDS))
            )
        await session.commit()
    _CREATED_USERNAMES.clear()
    _CREATED_NODE_IDS.clear()


def _run(coro):
    """Run a scenario in a fresh event loop, disposing DB engines on the way out.

    The engines are module-level singletons; without the ``finally`` dispose a
    pool connection created in this loop would be reused by the next test's
    (different) loop and die with ``Event loop is closed`` on Windows. Tracked
    rows are removed before the engines are disposed.
    """

    async def _runner():
        try:
            await coro
        finally:
            try:
                await _cleanup_tracked_rows()
            finally:
                await dispose_engines()

    return asyncio.run(_runner())


async def _create_user(username: str, role: str = "user", password: str = "test-pass-1") -> User:
    """Persist a user directly (``register()`` always creates ``role='user'``)."""
    _CREATED_USERNAMES.add(username)
    async with AsyncSessionLocal() as session:
        user = User(
            username=username,
            email=f"{username}@test.local",
            password_hash=hash_password(password, rounds=4),
            role=role,
            is_active=True,
            notification_prefs={},
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _seed_node_with_readings(node_id: str, n: int = 40) -> None:
    """Persist an active node plus ``n`` 30-min-spaced readings over ~20h."""
    _CREATED_NODE_IDS.add(node_id)
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        session.add(
            Node(
                node_id=node_id,
                name="Test Node",
                location_name="Test Lab",
                reading_interval=30,
                is_active=True,
            )
        )
        for i in range(n):
            session.add(
                SensorReading(
                    time=now - timedelta(minutes=i * 30),
                    node_id=node_id,
                    temperature=25.0 + i * 0.1,
                    humidity=60.0,
                    pm25=35.0,
                    pm10=50.0,
                    aqi=100 + i,
                    aqi_category="Moderate",
                    fuzzy_score=50.0,
                    is_anomaly=False,
                )
            )
        await session.commit()


# ── Auth: register / login / refresh rotation / logout (M-16) ─────────────────


def test_register_login_refresh_logout_rotation():
    """Full token lifecycle: register → login → rotate → reuse rejected → logout."""

    async def scenario():
        app = create_app()
        username = _unique("alice")
        _CREATED_USERNAMES.add(username)
        password = "s3cret-pass"
        async with app.test_client() as client:
            # register → 201, token pair (auto-login)
            reg = await client.post(
                f"{API}/auth/register",
                json={"username": username, "email": f"{username}@example.com", "password": password},
            )
            assert reg.status_code == 201
            reg_body = await reg.get_json()
            assert reg_body["role"] == "user"
            assert reg_body["user"]["username"] == username
            assert reg_body["access_token"] and reg_body["refresh_token"]
            assert reg_body["expires_in"] > 0
            access1, refresh1 = reg_body["access_token"], reg_body["refresh_token"]

            # login → 201 (docs contract, L-35)
            login = await client.post(
                f"{API}/auth/login", json={"username": username, "password": password}
            )
            assert login.status_code == 201
            assert (await login.get_json())["refresh_token"]

            # refresh → 200; the pair rotates (new refresh token, fresh access JWT)
            rot_resp = await client.post(f"{API}/auth/refresh", json={"refresh_token": refresh1})
            assert rot_resp.status_code == 200
            rotated = await rot_resp.get_json()
            assert rotated["refresh_token"] != refresh1
            assert decode_access_token(rotated["access_token"])["sub"] == reg_body["user"]["id"]

            # the consumed refresh token is revoked → 401
            reuse = await client.post(f"{API}/auth/refresh", json={"refresh_token": refresh1})
            assert reuse.status_code == 401
            err = await reuse.get_json()
            assert err["status"] == 401 and err["title"] == "Unauthorized"

            # logout the rotated token → 204; logging out twice stays 204
            logout = await client.post(
                f"{API}/auth/logout", json={"refresh_token": rotated["refresh_token"]}
            )
            assert logout.status_code == 204
            logout2 = await client.post(
                f"{API}/auth/logout", json={"refresh_token": rotated["refresh_token"]}
            )
            assert logout2.status_code == 204

            # the logged-out token can no longer rotate
            dead = await client.post(
                f"{API}/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
            )
            assert dead.status_code == 401

    _run(scenario())


def test_auth_error_contract():
    """Validation errors are RFC 7807 problem+json with the right status."""

    async def scenario():
        app = create_app()
        async with app.test_client() as client:
            # bad email → 422
            bad = await client.post(
                f"{API}/auth/register",
                json={"username": _unique("joe"), "email": "not-an-email", "password": "pass-123"},
            )
            assert bad.status_code == 422
            body = await bad.get_json()
            assert body["status"] == 422 and body["title"] == "Unprocessable Entity"
            assert "application/problem+json" in bad.headers.get("Content-Type", "")

            # duplicate username → 409
            name = _unique("dup")
            _CREATED_USERNAMES.add(name)
            first = await client.post(
                f"{API}/auth/register",
                json={"username": name, "email": f"{name}@example.com", "password": "pass-123"},
            )
            assert first.status_code == 201
            second = await client.post(
                f"{API}/auth/register",
                json={"username": name, "email": f"{name}@example.com", "password": "pass-123"},
            )
            assert second.status_code == 409
            assert (await second.get_json())["title"] == "Conflict"

            # wrong password → 401
            wrong = await client.post(
                f"{API}/auth/login", json={"username": name, "password": "wrong-pass"}
            )
            assert wrong.status_code == 401

            # missing body → 400
            missing = await client.post(f"{API}/auth/login", json=None)
            assert missing.status_code == 400

    _run(scenario())


# ── Profile: CRUD + change-password (M-16) ─────────────────────────────────────


def test_profile_lifecycle_and_change_password():
    """GET/PATCH/change-password/DELETE on /profile, plus re-login effects."""

    async def scenario():
        app = create_app()
        username = _unique("bob")
        _CREATED_USERNAMES.add(username)
        password = "first-pass"
        async with app.test_client() as client:
            reg = await client.post(
                f"{API}/auth/register",
                json={"username": username, "email": f"{username}@example.com", "password": password},
            )
            assert reg.status_code == 201
            token = (await reg.get_json())["access_token"]
            headers = _auth_headers(token)

            # GET → 200
            got = await client.get(f"{API}/profile", headers=headers)
            assert got.status_code == 200
            prof = await got.get_json()
            assert prof["username"] == username and prof["role"] == "user"

            # PATCH email → 200 and persisted
            new_email = f"{username}-new@example.com"
            patched = await client.patch(f"{API}/profile", json={"email": new_email}, headers=headers)
            assert patched.status_code == 200
            assert (await patched.get_json())["email"] == new_email

            # change-password with wrong current → 401
            wrong = await client.post(
                f"{API}/profile/change-password",
                json={"current_password": "nope-nope", "new_password": "brand-new-pass"},
                headers=headers,
            )
            assert wrong.status_code == 401

            # change-password with correct current → 200
            ok = await client.post(
                f"{API}/profile/change-password",
                json={"current_password": password, "new_password": "brand-new-pass"},
                headers=headers,
            )
            assert ok.status_code == 200

            # old password now rejected, new password accepted
            old_login = await client.post(
                f"{API}/auth/login", json={"username": username, "password": password}
            )
            assert old_login.status_code == 401
            new_login = await client.post(
                f"{API}/auth/login",
                json={"username": username, "password": "brand-new-pass"},
            )
            assert new_login.status_code == 201

            # DELETE deactivates → the same access token is now rejected
            deleted = await client.delete(f"{API}/profile", headers=headers)
            assert deleted.status_code == 200
            gone = await client.get(f"{API}/profile", headers=headers)
            assert gone.status_code == 401

    _run(scenario())


# ── Readings: latest / history (M-16) + M-15 span clamp ───────────────────────


def test_readings_latest_history_and_error_contract():
    """Latest requires auth; history returns time-bucketed aggregates."""

    async def scenario():
        app = create_app()
        node_id = _unique("NODE")
        await _seed_node_with_readings(node_id, n=40)
        user = await _create_user(_unique("reader"))
        token = create_access_token(user.id, user.role)

        now = datetime.now(timezone.utc)
        async with app.test_client() as client:
            # unauthenticated → 401 problem+json, but rate-limit headers attached
            # (rate_limit runs before jwt_required)
            anon = await client.get(f"{API}/readings/latest")
            assert anon.status_code == 401
            anon_body = await anon.get_json()
            assert anon_body["status"] == 401
            assert "application/problem+json" in anon.headers.get("Content-Type", "")
            assert anon.headers.get("X-RateLimit-Limit") == "200"

            # latest → 200, our active node is present
            latest = await client.get(f"{API}/readings/latest", headers=_auth_headers(token))
            assert latest.status_code == 200
            readings = (await latest.get_json())["readings"]
            assert any(r["node_id"] == node_id for r in readings)

            # history → 200, hour-bucketed aggregates for the node
            url = (
                f"{API}/readings/history?node_id={node_id}&bucket=1h"
                f"&from={_iso_utc(now - timedelta(days=2))}&to={_iso_utc(now)}"
            )
            hist = await client.get(url, headers=_auth_headers(token))
            assert hist.status_code == 200
            buckets = (await hist.get_json())["buckets"]
            assert len(buckets) >= 1
            assert all(b["node_id"] == node_id for b in buckets)
            assert all(b["reading_count"] >= 1 for b in buckets)

            # invalid bucket → 422
            bad = await client.get(
                f"{API}/readings/history?bucket=99m", headers=_auth_headers(token)
            )
            assert bad.status_code == 422
            assert (await bad.get_json())["status"] == 422

            # reversed range → 422
            rev = await client.get(
                f"{API}/readings/history?from={_iso_utc(now)}&to={_iso_utc(now - timedelta(hours=1))}",
                headers=_auth_headers(token),
            )
            assert rev.status_code == 422

    _run(scenario())


def test_history_range_is_clamped_per_bucket(timescale_available):
    """M-15: a fine bucket clamps the span; a coarse bucket does not."""
    if not timescale_available:
        pytest.skip("timescaledb not available in test DB — cannot run time_bucket")

    async def scenario():
        app = create_app()
        node_id = _unique("CLAMP")
        _CREATED_NODE_IDS.add(node_id)
        now = datetime.now(timezone.utc)
        # One reading well within the 1m span, one well beyond it (40 days ago).
        async with AsyncSessionLocal() as session:
            session.add(
                Node(node_id=node_id, name="Clamp", reading_interval=30, is_active=True)
            )
            session.add(
                SensorReading(
                    time=now - timedelta(days=5), node_id=node_id, pm25=10.0,
                    aqi=50, aqi_category="Good", is_anomaly=False,
                )
            )
            session.add(
                SensorReading(
                    time=now - timedelta(days=40), node_id=node_id, pm25=99.0,
                    aqi=150, aqi_category="Unhealthy", is_anomaly=False,
                )
            )
            await session.commit()

        user = await _create_user(_unique("clampreader"))
        headers = _auth_headers(create_access_token(user.id, user.role))
        far_from = _iso_utc(now - timedelta(days=60))

        async with app.test_client() as client:
            # 1m: span clamped to 30 days → the 40-day-old reading is excluded
            fine = await client.get(
                f"{API}/readings/history?node_id={node_id}&bucket=1m"
                f"&from={far_from}&to={_iso_utc(now)}",
                headers=headers,
            )
            assert fine.status_code == 200
            fine_buckets = (await fine.get_json())["buckets"]
            cutoff = now - timedelta(days=31)
            for b in fine_buckets:
                assert datetime.fromisoformat(b["bucket"].replace("Z", "+00:00")) >= cutoff
            assert any(
                datetime.fromisoformat(b["bucket"].replace("Z", "+00:00"))
                >= now - timedelta(days=6)
                for b in fine_buckets
            )

            # 1d: span allows 10 years → both the 5-day and 40-day readings appear
            coarse = await client.get(
                f"{API}/readings/history?node_id={node_id}&bucket=1d"
                f"&from={far_from}&to={_iso_utc(now)}",
                headers=headers,
            )
            assert coarse.status_code == 200
            coarse_buckets = (await coarse.get_json())["buckets"]
            assert len(coarse_buckets) >= 2
            assert any(
                datetime.fromisoformat(b["bucket"].replace("Z", "+00:00"))
                >= now - timedelta(days=6)
                for b in coarse_buckets
            )
            assert any(
                datetime.fromisoformat(b["bucket"].replace("Z", "+00:00"))
                <= now - timedelta(days=38)
                for b in coarse_buckets
            )

    _run(scenario())


# ── Forecast (M-16) ────────────────────────────────────────────────────────────


def test_forecast_endpoint_contract():
    """Forecast echoes node_id; unknown node 404s; missing node_id 422s."""

    async def scenario():
        app = create_app()
        node_id = _unique("FCST")
        # <30 readings → no trainable model → generate_forecast returns [] (valid).
        await _seed_node_with_readings(node_id, n=5)
        user = await _create_user(_unique("forecaster"))
        headers = _auth_headers(create_access_token(user.id, user.role))

        async with app.test_client() as client:
            missing = await client.get(f"{API}/forecast?node_id={_unique('NOPE')}", headers=headers)
            assert missing.status_code == 404
            assert (await missing.get_json())["title"] == "Not Found"

            resp = await client.get(f"{API}/forecast?node_id={node_id}", headers=headers)
            assert resp.status_code == 200
            body = await resp.get_json()
            assert body["node_id"] == node_id
            assert body["horizon_minutes"] == 60
            assert isinstance(body["points"], list)

            bad = await client.get(f"{API}/forecast", headers=headers)
            assert bad.status_code == 422
            assert (await bad.get_json())["status"] == 422

    _run(scenario())


# ── Rate limiting (M-12 machinery) ─────────────────────────────────────────────


def test_rate_limit_returns_429_on_breach(monkeypatch):
    """A window breach returns RFC 7807 429 with the per-endpoint limit."""

    class _DummyClient:
        pass

    # Restore a non-None client for this test so the breach path is reachable.
    monkeypatch.setattr("api.rate_limit.get_client", lambda: _DummyClient())

    async def _breach(client, key: str, window_seconds: int) -> int:
        return 11  # login limit is 10/min

    monkeypatch.setattr("api.rate_limit._incr", _breach)

    async def scenario():
        app = create_app()
        async with app.test_client() as client:
            resp = await client.post(
                f"{API}/auth/login", json={"username": "x", "password": "y"}
            )
            assert resp.status_code == 429
            body = await resp.get_json()
            assert body["status"] == 429 and body["title"] == "Too Many Requests"
            assert resp.headers.get("X-RateLimit-Limit") == "10"
            assert int(resp.headers["X-RateLimit-Remaining"]) <= 0

    _run(scenario())


# ── L-27 · expired token is never claimed ──────────────────────────────────────


def test_expired_token_is_not_claimed_by_refresh():
    """L-27: refresh excludes expired tokens, so they are never revoked.

    The ``UPDATE ... RETURNING`` in ``/auth/refresh`` excludes rows whose
    ``expires_at`` is already past, so an expired token falls through to the
    generic 401 and its row is left un-revoked — the same expired token cannot
    be rotated forever (and, being unclaimed, it stays inert in the DB).
    """

    async def scenario():
        user = await _create_user(_unique("expiryer"))
        raw, token_hash = generate_refresh_token()
        async with AsyncSessionLocal() as session:
            session.add(
                RefreshToken(
                    user_id=user.id,
                    token_hash=token_hash,
                    expires_at=datetime.now(timezone.utc) - timedelta(days=1),
                )
            )
            await session.commit()

        app = create_app()
        async with app.test_client() as client:
            resp = await client.post(
                f"{API}/auth/refresh", json={"refresh_token": raw}
            )
            assert resp.status_code == 401
            assert (await resp.get_json())["title"] == "Unauthorized"

        # The expired token must NOT have been claimed: still present and
        # un-revoked (the UPDATE's WHERE excluded it).
        async with AsyncSessionLocal() as session:
            rt = await session.scalar(
                select(RefreshToken).where(RefreshToken.token_hash == token_hash)
            )
            assert rt is not None
            assert rt.revoked is False

    _run(scenario())


# ── L-29 · forecast rejects inactive nodes ─────────────────────────────────────


def test_forecast_inactive_node_404():
    """L-29: an inactive node gets a 404 forecast, matching /readings/latest.

    Before the fix the existence check omitted ``is_active``, so a deactivated
    node could still get an on-the-fly forecast.
    """

    async def scenario():
        app = create_app()
        inactive_id = _unique("OFF")
        _CREATED_NODE_IDS.add(inactive_id)
        async with AsyncSessionLocal() as session:
            session.add(
                Node(
                    node_id=inactive_id,
                    name="Deactivated",
                    reading_interval=30,
                    is_active=False,
                )
            )
            await session.commit()

        user = await _create_user(_unique("fuser"))
        headers = _auth_headers(create_access_token(user.id, user.role))

        async with app.test_client() as client:
            resp = await client.get(
                f"{API}/forecast?node_id={inactive_id}", headers=headers
            )
            assert resp.status_code == 404
            assert (await resp.get_json())["title"] == "Not Found"

    _run(scenario())


# ── L-30 · Authorization scheme is case-insensitive ────────────────────────────


def test_auth_scheme_case_insensitive():
    """L-30: RFC 7235 auth-scheme names are case-insensitive.

    A lowercase ``bearer <token>`` header must be accepted exactly like
    ``Bearer <token>``; a genuinely different scheme still 401s.
    """

    async def scenario():
        user = await _create_user(_unique("lower"))
        token = create_access_token(user.id, user.role)

        app = create_app()
        async with app.test_client() as client:
            up = await client.get(f"{API}/profile", headers=_auth_headers(token))
            assert up.status_code == 200

            low = await client.get(
                f"{API}/profile", headers={"Authorization": f"bearer {token}"}
            )
            assert low.status_code == 200

            bad = await client.get(
                f"{API}/profile", headers={"Authorization": f"Basic {token}"}
            )
            assert bad.status_code == 401

    _run(scenario())


# ── L-31 · rate-limit INCR + EXPIRE is atomic via Lua ──────────────────────────


def test_rate_limit_incr_is_atomic():
    """L-31: INCR + conditional EXPIRE run as a single Lua eval, not two steps.

    Redis is not available in this environment, so we assert the call pattern
    against a recording fake client: ``_incr`` must issue exactly one ``eval``
    (containing INCR + PEXPIRE) with the window in ms, rather than separate
    ``incr`` + ``expire`` round-trips that could leave a key with no TTL if the
    process died in between.
    """
    from api.rate_limit import _incr

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
        assert ttl_ms == 60_000  # window_seconds * 1000, passed to PEXPIRE

    _run(scenario())


# ── Admin guard order-independence (M-11) ──────────────────────────────────────


def test_admin_required_order_independent():
    """@admin_required works stacked above OR below @jwt_required (M-11)."""

    async def scenario():
        admin = await _create_user(_unique("adminx"), role="admin")
        regular = await _create_user(_unique("userx"), role="user")

        app = Quart(__name__)

        @app.route("/admin-up")
        @admin_required
        @jwt_required
        async def admin_up():
            return jsonify({"ok": True}), 200

        @app.route("/admin-down")
        @jwt_required
        @admin_required
        async def admin_down():
            return jsonify({"ok": True}), 200

        admin_token = create_access_token(admin.id, "admin")
        user_token = create_access_token(regular.id, "user")

        async with app.test_client() as client:
            # admin passes in both stack orders
            assert (await client.get("/admin-up", headers=_auth_headers(admin_token))).status_code == 200
            assert (await client.get("/admin-down", headers=_auth_headers(admin_token))).status_code == 200

            # non-admin → 403 in both orders
            assert (await client.get("/admin-up", headers=_auth_headers(user_token))).status_code == 403
            assert (await client.get("/admin-down", headers=_auth_headers(user_token))).status_code == 403

            # unauthenticated → 401
            assert (await client.get("/admin-up")).status_code == 401
            assert (await client.get("/admin-down")).status_code == 401

    _run(scenario())


# ── Schema guards (M-13 / M-14) ────────────────────────────────────────────────


def test_password_byte_length_enforced():
    """M-14: bcrypt truncates at 72 *bytes*, not 72 chars — schema enforces bytes."""
    from pydantic import ValidationError

    from api.schemas import ChangePasswordRequest, LoginRequest, RegisterRequest

    ok = "é" * 36  # exactly 72 UTF-8 bytes
    assert len(ok.encode("utf-8")) == 72
    RegisterRequest(username="byteuser", email="byteuser@example.com", password=ok)
    LoginRequest(username="byteuser", password=ok)
    ChangePasswordRequest(current_password=ok, new_password=ok)

    too_long = "é" * 37  # 74 bytes — over the limit despite 37 chars
    assert len(too_long.encode("utf-8")) == 74
    with pytest.raises(ValidationError):
        RegisterRequest(username="byteuser", email="byteuser@example.com", password=too_long)
    with pytest.raises(ValidationError):
        LoginRequest(username="byteuser", password=too_long)
    with pytest.raises(ValidationError):
        ChangePasswordRequest(current_password=too_long, new_password=ok)


def test_refresh_token_length_capped():
    """M-13: RefreshRequest caps token length in the schema."""
    from pydantic import ValidationError

    from api.schemas import RefreshRequest

    RefreshRequest(refresh_token="x" * 256)  # exactly at the cap
    with pytest.raises(ValidationError):
        RefreshRequest(refresh_token="x" * 257)


# ── Misc status contract (M-16) ────────────────────────────────────────────────


def test_health_endpoint():
    """/health reports ok without auth."""

    async def scenario():
        app = create_app()
        async with app.test_client() as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            body = await resp.get_json()
            assert body["status"] == "ok"

    _run(scenario())


# ── L-34 · no unauthenticated default /static route ───────────────────────────


def test_create_app_registers_no_static_route():
    """L-34: the app must not expose Quart's default /static/<filename> route.

    ``Quart(__name__)`` registers an unauthenticated static-file route; serving
    files from a later-created static/ dir would bypass auth. ``static_folder=None``
    removes it, so ``url_map`` must contain no rule whose path starts with /static.
    """
    app = create_app()
    assert not any(rule.rule.startswith("/static") for rule in app.url_map.iter_rules())


def test_request_body_size_capped():
    """M-13: an oversized request body is rejected with 413."""

    async def scenario():
        app = create_app()
        async with app.test_client() as client:
            big = {"username": "x", "password": "p" + "a" * 70_000}
            resp = await client.post(f"{API}/auth/login", json=big)
            assert resp.status_code == 413

    _run(scenario())
