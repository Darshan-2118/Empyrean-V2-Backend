"""
Request-validation middleware tests (Phase 12, ``@validate_body``).

Every JSON-body endpoint must reject malformed bodies with RFC 7807
``422 Unprocessable Entity`` (``application/problem+json``) and accept a valid
body — never a 500 and never a bare pydantic error. Covers the auth endpoints
(register/login/refresh/logout), profile PATCH + change-password, nodes
POST/PATCH, and admin settings PATCH. The missing-body branch (``400``) and the
empty-``{}``-is-a-422 branch (schema validation, not "missing body") are
asserted too, since those status-code distinctions are part of the documented
contract (docs/api.md).

Conventions mirror ``tests/test_export.py``: committed seed rows use unique ids
(the ``empyrean_test`` tables are dropped at session end), Redis is patched to
the fail-open ``None`` client so auth rate-limit buckets never block, and async
scenarios run through :func:`_run` on a fresh event loop.

Note on "unknown key": pydantic v2 ignores unknown keys by default, so auth /
profile / nodes bodies with an extra key validate normally (201/200). Only
``AdminSettingsUpdate`` sets ``extra="forbid"`` — the typo-protection 422 for
an unknown key is asserted against the admin settings PATCH, which is exactly
where the schema enforces it.
"""

from __future__ import annotations

import asyncio
import secrets

import pytest

from app import create_app
from api.jwt import create_access_token
from models import Node, User
from models.base import async_engine, get_sync_db
from models.helpers import hash_password


@pytest.fixture(autouse=True)
def _fast_redis_down(monkeypatch):
    """Simulate an unreachable Redis fast (documented fail-open path).

    Auth endpoints are rate-limited; with no Redis server the ops would block
    for the OS connect timeout. Patching the client to ``None`` makes them fast
    no-ops (see api/rate_limit.py docs) without changing behaviour.
    """
    monkeypatch.setattr("api.rate_limit.get_client", lambda: None)
    monkeypatch.setattr("api.cache.get_client", lambda: None)


def _run(coro):
    """Run an async scenario on a fresh loop, then dispose the async pool."""

    async def _wrapped():
        try:
            return await coro
        finally:
            await async_engine.dispose()

    return asyncio.run(_wrapped())


def _unique(prefix: str) -> str:
    """Return a unique slug for test-scoped rows (never collide across modules)."""
    return f"{prefix}_{secrets.token_hex(4)}"


def _seed_user(prefix: str, *, role: str = "user") -> int:
    """Create a committed user via the sync pipeline; return its id."""
    username = _unique(prefix)
    with get_sync_db() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("secret-pass-123", rounds=4),
            role=role,
            is_active=True,
            notification_prefs={},
        )
        session.add(user)
        session.flush()
        return user.id


def _seed_admin(prefix: str) -> int:
    """Create a committed admin user; return its id."""
    return _seed_user(prefix, role="admin")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _assert_problem(resp, status: int) -> None:
    """Assert an RFC 7807 problem+json response with the given status."""
    body = await resp.get_json()
    assert resp.status_code == status
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert "application/problem+json" in resp.headers.get("Content-Type", "")


# ── Auth: register / login / refresh ──────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "joe", "password": "pass-123"},           # missing email
        {"email": "joe@example.com", "password": "pass-123"},  # missing username
        {"username": "joe", "email": "joe@example.com"},       # missing password
        {"username": 123, "email": "joe@example.com", "password": "pass-123"},  # wrong type
        {"username": "joe", "email": "not-an-email", "password": "pass-123"},   # bad email
        {"username": "jo", "email": "joe@example.com", "password": "pass-123"},  # username < 3
        {"username": "joe", "email": "joe@example.com", "password": "abc"},      # password < 6
        {"username": "joe", "email": "joe@example.com", "password": "é" * 37},   # > 72 bytes
    ],
)
def test_register_malformed_bodies_return_422(payload):
    """Every malformed register body is an RFC 7807 422, never a 500."""

    async def _scenario():
        client = create_app().test_client()
        resp = await client.post("/api/v1/auth/register", json=payload)
        await _assert_problem(resp, 422)
        body = await resp.get_json()
        assert body["title"] == "Unprocessable Entity"

    _run(_scenario())


def test_register_missing_body_is_400_and_empty_object_is_422():
    """Missing/malformed JSON body → 400; an empty {} is schema-validated → 422."""

    async def _scenario():
        client = create_app().test_client()

        # no body at all → 400 (the documented missing-body branch)
        resp = await client.post("/api/v1/auth/register", json=None)
        await _assert_problem(resp, 400)
        body = await resp.get_json()
        assert body["title"] == "Bad Request"

        # a well-formed empty object falls through to schema validation → 422
        resp = await client.post("/api/v1/auth/register", json={})
        await _assert_problem(resp, 422)

        # a JSON array body (not an object) is rejected by pydantic → 422
        resp = await client.post("/api/v1/auth/register", json=["joe"])
        await _assert_problem(resp, 422)

    _run(_scenario())


def test_register_and_login_valid_bodies_succeed():
    """A valid register body 201s and auto-login credentials still login."""

    async def _scenario():
        username = _unique("rvuser")
        password = "valid-pass-1"
        client = create_app().test_client()

        reg = await client.post("/api/v1/auth/register", json={
            "username": username, "email": f"{username}@example.com", "password": password,
        })
        assert reg.status_code == 201
        reg_body = await reg.get_json()
        assert reg_body["access_token"] and reg_body["refresh_token"]

        login = await client.post("/api/v1/auth/login", json={
            "username": username, "password": password,
        })
        assert login.status_code == 201
        assert (await login.get_json())["access_token"]

    _run(_scenario())


def test_login_and_refresh_malformed_bodies_return_422():
    """login/refresh reject a missing-required-field body with 422."""

    async def _scenario():
        client = create_app().test_client()

        login = await client.post("/api/v1/auth/login", json={"username": "x"})
        await _assert_problem(login, 422)

        refresh = await client.post("/api/v1/auth/refresh", json={})
        await _assert_problem(refresh, 422)

    _run(_scenario())


# ── Nodes: POST / PATCH ───────────────────────────────────────────────────────


def test_nodes_post_malformed_body_422_and_valid_201():
    """POST /nodes: bad node_id is a 422; a valid body registers the node."""

    async def _scenario():
        user_id = _seed_user("rvnode")
        client = create_app().test_client()
        headers = _auth(create_access_token(user_id, "user"))

        # node_id with illegal characters (spaces / wildcards) → 422
        bad = await client.post(
            "/api/v1/nodes", headers=headers, json={"node_id": "bad id!"}
        )
        await _assert_problem(bad, 422)

        # reading_interval out of range → 422
        bad = await client.post(
            "/api/v1/nodes", headers=headers,
            json={"node_id": "RV-OK", "reading_interval": 0},
        )
        await _assert_problem(bad, 422)

        # valid body → 201
        node_id = f"RV-{secrets.token_hex(3).upper()}"
        ok = await client.post("/api/v1/nodes", headers=headers, json={
            "node_id": node_id, "name": "Phase12 node", "reading_interval": 60,
        })
        assert ok.status_code == 201
        body = await ok.get_json()
        assert body["node_id"] == node_id and body["reading_interval"] == 60

    _run(_scenario())


def test_nodes_patch_malformed_body_422_and_valid_200():
    """PATCH /nodes/:node_id: a wrong-typed field is a 422; a valid body applies."""

    async def _scenario():
        admin_id = _seed_admin("rvnode")
        client = create_app().test_client()
        headers = _auth(create_access_token(admin_id, "admin"))
        node_id = f"RVP-{secrets.token_hex(3).upper()}"
        with get_sync_db() as session:
            session.add(Node(node_id=node_id, name="Before", reading_interval=30, is_active=True))

        # reading_interval must be an int (string rejected under pydantic v2 lax → 422)
        bad = await client.patch(
            f"/api/v1/nodes/{node_id}", headers=headers, json={"reading_interval": "soon"}
        )
        await _assert_problem(bad, 422)

        ok = await client.patch(
            f"/api/v1/nodes/{node_id}", headers=headers, json={"name": "After"}
        )
        assert ok.status_code == 200
        assert (await ok.get_json())["name"] == "After"

    _run(_scenario())


# ── Admin settings PATCH ──────────────────────────────────────────────────────


def test_admin_settings_patch_unknown_key_422_and_valid_200():
    """PATCH /admin/settings: unknown key → 422 (extra=forbid); valid body → 200.

    Settings rows are global singletons (key-based PK), so any row this test
    writes is removed in ``finally`` — later tests must see the same empty
    ``system_settings`` table (mirrors test_phase_coverage's admin cleanup).
    """
    from sqlalchemy import delete

    from models import SystemSetting

    async def _scenario():
        admin_id = _seed_admin("rvadmin")
        client = create_app().test_client()
        headers = _auth(create_access_token(admin_id, "admin"))

        # unknown key → 422 (typo protection)
        bad = await client.patch(
            "/api/v1/admin/settings", headers=headers, json={"bogus_key": 1}
        )
        await _assert_problem(bad, 422)

        # non-object body → 422 (require_object)
        bad = await client.patch("/api/v1/admin/settings", headers=headers, json=[1, 2])
        await _assert_problem(bad, 422)

        try:
            ok = await client.patch(
                "/api/v1/admin/settings", headers=headers, json={"aqi_warning_threshold": 90}
            )
            assert ok.status_code == 200
            settings = (await ok.get_json())["settings"]
            assert next(
                s for s in settings if s["key"] == "aqi_warning_threshold"
            )["value"] == "90"
        finally:
            with get_sync_db() as session:
                session.execute(
                    delete(SystemSetting).where(SystemSetting.key == "aqi_warning_threshold")
                )
                session.commit()

    _run(_scenario())


# ── Profile: PATCH + change-password ──────────────────────────────────────────


def test_profile_patch_malformed_body_422_and_valid_200():
    """PATCH /profile: bad email is a 422; a valid body updates the profile."""

    async def _scenario():
        user_id = _seed_user("rvprof")
        client = create_app().test_client()
        headers = _auth(create_access_token(user_id, "user"))

        bad = await client.patch(
            "/api/v1/profile", headers=headers, json={"email": "not-an-email"}
        )
        await _assert_problem(bad, 422)

        new_email = f"{_unique('rvmail')}@example.com"
        ok = await client.patch("/api/v1/profile", headers=headers, json={"email": new_email})
        assert ok.status_code == 200
        assert (await ok.get_json())["email"] == new_email

    _run(_scenario())


def test_change_password_malformed_body_422_and_valid_200():
    """change-password: short new_password is a 422; a valid body changes it."""

    async def _scenario():
        user_id = _seed_user("rvpass")
        client = create_app().test_client()
        headers = _auth(create_access_token(user_id, "user"))

        # new_password below the 6-char minimum → 422
        bad = await client.post(
            "/api/v1/profile/change-password", headers=headers,
            json={"current_password": "secret-pass-123", "new_password": "abc"},
        )
        await _assert_problem(bad, 422)

        ok = await client.post(
            "/api/v1/profile/change-password", headers=headers,
            json={"current_password": "secret-pass-123", "new_password": "new-pass-99"},
        )
        assert ok.status_code == 200

    _run(_scenario())
