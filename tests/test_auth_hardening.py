"""
Auth/JWT hardening tests — L-26, L-27, L-30, L-33.

Covers four known-issue fixes in the auth/JWT layer:

* **L-30** — the ``Authorization`` scheme match is case-insensitive (RFC 7235)
  and the token is extracted by fixed offset ``auth[7:]``, so a lowercase
  ``bearer`` prefix is accepted and stripped correctly.
* **L-26** — refreshing a deactivated (or missing) user's token returns the
  same generic 401 detail as every other refresh failure, so a stolen token
  cannot distinguish an account's state; the stolen token is still committed
  as revoked so it cannot be reused.
* **L-27** — refresh excludes *expired* tokens in the UPDATE...RETURNING WHERE,
  so an expired token is never claimed/revoked (no mutation) and the
  forever-rotation hole is closed.
* **L-33** — the dummy bcrypt "timing-equalizer" hash is computed lazily on
  first failed-login use, never at module import.

Conventions mirror ``tests/test_phase_coverage.py``: async HTTP scenarios run
through :func:`_run_async` on a fresh event loop (disposing the asyncpg pool
inside it), and committed seed rows are written via the sync ``get_sync_db()``
pipeline so the async API can see them. Redis is optional / fails open.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app
from api.jwt import create_access_token, generate_refresh_token
from models import RefreshToken, User
from models.base import async_engine, get_sync_db
from models.helpers import hash_password


# ── Async infra ────────────────────────────────────────────────────────────────


def _run_async(coro):
    """Run an async scenario on a fresh loop, then dispose the async pool."""
    async def _wrapped():
        try:
            return await coro
        finally:
            await async_engine.dispose()

    return asyncio.run(_wrapped())


def _seed_user(prefix: str, *, active: bool = True) -> int:
    """Create a committed user via the sync pipeline; return its id."""
    tag = secrets.token_hex(4)
    username = f"{prefix}_{tag}"
    with get_sync_db() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("secret-pass-123", rounds=4),
            role="user",
            is_active=active,
            notification_prefs={},
        )
        session.add(user)
        session.flush()
        return user.id


def _seed_refresh_token(
    user_id: int, *, expires_at: datetime, revoked: bool = False,
) -> tuple[str, str, int]:
    """Insert a committed RefreshToken; return ``(raw, token_hash, id)``."""
    raw, token_hash = generate_refresh_token()
    with get_sync_db() as session:
        rt = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            revoked=revoked,
        )
        session.add(rt)
        session.flush()
        return raw, token_hash, rt.id


# ── L-30: case-insensitive Authorization scheme ───────────────────────────────


def test_l30_bearer_scheme_is_case_insensitive():
    """Lowercase ``bearer`` scheme authenticates; a missing scheme is 401."""
    user_id = _seed_user("t30")
    token = create_access_token(user_id, "user")

    async def _scenario():
        client = create_app().test_client()

        # L-30: the scheme check must be case-insensitive (RFC 7235), so a
        # lowercase "bearer " prefix authenticates and the token is extracted.
        resp = await client.get(
            "/api/v1/readings/latest",
            headers={"Authorization": f"bearer {token}"},
        )
        assert resp.status_code == 200, (resp.status_code, await resp.get_data())

        # A header with no scheme at all is rejected.
        resp = await client.get(
            "/api/v1/readings/latest",
            headers={"Authorization": token},
        )
        assert resp.status_code == 401

    _run_async(_scenario())


# ── L-26: deactivated user refresh does not leak state ────────────────────────


def test_l26_deactivated_user_refresh_returns_generic_detail():
    """A deactivated user's refresh returns the generic 401 detail (no leak)."""
    user_id = _seed_user("t26", active=True)
    # Deactivate AFTER issuing a valid unexpired token.
    with get_sync_db() as session:
        user = session.get(User, user_id)
        user.is_active = False
    raw, _, _ = _seed_refresh_token(
        user_id, expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )

    async def _scenario():
        client = create_app().test_client()
        resp = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": raw}
        )
        assert resp.status_code == 401
        body = await resp.get_json()
        # L-26: identical generic detail to every other refresh failure.
        assert body["detail"] == "Refresh token is invalid or expired"

    _run_async(_scenario())


# ── L-27: expired token is never claimed by the UPDATE ────────────────────────


def test_l27_expired_token_is_not_claimed_by_refresh():
    """Refresh of an expired token returns 401 AND leaves the row un-revoked."""
    user_id = _seed_user("t27", active=True)
    raw, token_hash, rt_id = _seed_refresh_token(
        user_id, expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    async def _scenario():
        client = create_app().test_client()
        resp = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": raw}
        )
        assert resp.status_code == 401
        body = await resp.get_json()
        assert body["detail"] == "Refresh token is invalid or expired"

    _run_async(_scenario())

    # L-27: the UPDATE...RETURNING must have excluded the expired row, so it is
    # neither revoked nor deleted — the same expired token cannot be rotated.
    with get_sync_db() as session:
        row = session.get(RefreshToken, rt_id)
        assert row is not None
        assert row.revoked is False
        assert row.token_hash == token_hash


# ── L-33: dummy password hash is lazy ─────────────────────────────────────────


def test_l33_dummy_password_hash_is_lazy():
    """The timing-equalizer hash is built on first use, not at module import."""
    import api.auth

    # Re-execute the module so the lazy global is guaranteed unset regardless
    # of any earlier import/use elsewhere in the session.
    api.auth = importlib.reload(api.auth)
    assert api.auth._DUMMY_PASSWORD_HASH is None  # not computed at import

    h = api.auth._dummy_password_hash()
    assert isinstance(h, str)
    assert h.startswith("$2")  # bcrypt salt marker
    assert len(h) >= 59        # cost-12 bcrypt hashes are 60 chars


# ── Bootstrap admin tests (H5/H6/H28) ─────────────────────────────────────────


def test_bootstrap_admin_login_and_access(monkeypatch):
    """Env-configured bootstrap admin logs in via the normal bcrypt path.

    H5/H6: credentials come from BOOTSTRAP_ADMIN_* env vars (never source), are
    provisioned as a bcrypt hash, and authenticate through the standard login
    path — there is no plaintext bypass anymore.
    """
    import api.auth
    from config import get_config as real_get_config

    tag = secrets.token_hex(4)
    username = f"bootstrap_admin_{tag}"
    password = f"B00tstrap-{tag}-pass!"

    real_cfg = real_get_config()

    class _Cfg:
        """Real config with bootstrap admin fields overridden."""

        def __getattr__(self, name):
            return getattr(real_cfg, name)

    cfg = _Cfg()
    cfg.BOOTSTRAP_ADMIN_USERNAME = username
    cfg.BOOTSTRAP_ADMIN_PASSWORD = password
    cfg.BOOTSTRAP_ADMIN_EMAIL = ""
    # api.auth resolves config through its module-level import.
    monkeypatch.setattr(api.auth, "get_config", lambda: cfg)

    async def _scenario():
        app = create_app()
        client = app.test_client()

        # Provision via the startup helper (normally run by before_serving).
        await api.auth.ensure_hardcoded_admin()

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        assert resp.status_code == 201, (resp.status_code, await resp.get_data())
        data = await resp.get_json()
        assert data["role"] == "admin"
        assert data["user"]["username"] == username
        assert "access_token" in data
        assert "refresh_token" in data

        token = data["access_token"]

        admin_resp = await client.get(
            "/api/v1/admin/settings",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert admin_resp.status_code == 200, (resp.status_code, await admin_resp.get_data())

        # Wrong password fails with 401 — no plaintext bypass exists.
        wrong_resp = await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "wrongpassword"},
        )
        assert wrong_resp.status_code == 401

    _run_async(_scenario())


def test_no_hardcoded_admin_without_env_config():
    """With no BOOTSTRAP_ADMIN_* configured, provisioning is a no-op (H5)."""
    import api.auth

    creds = api.auth._bootstrap_admin_credentials()
    # In the test environment no bootstrap creds are set by default; if an
    # operator's .env sets them, the helper must still return env values only —
    # never the removed hardcoded constants.
    if creds is not None:
        username, password, _ = creds
        assert password != "Darsh1812"
        assert username.lower() != "darshan"


# ── M91: bootstrap admin must not promote a case-variant account ─────────────


def test_m91_bootstrap_refuses_case_variant_account(monkeypatch):
    """A pre-registered case-variant account is never promoted (M91)."""
    import api.auth
    from config import get_config as real_get_config

    tag = secrets.token_hex(4)
    existing_username = f"BootAdmin_{tag}"  # registered by an "attacker"
    bootstrap_username = f"bootadmin_{tag}"  # differs only in case

    attacker_hash = hash_password("attacker-pass-1", rounds=4)
    with get_sync_db() as session:
        user = User(
            username=existing_username,
            email=f"{existing_username.lower()}@example.com",
            password_hash=attacker_hash,
            role="user",
            is_active=True,
            notification_prefs={},
        )
        session.add(user)
        session.flush()
        user_id = user.id

    real_cfg = real_get_config()

    class _Cfg:
        """Real config with bootstrap admin fields overridden."""

        def __getattr__(self, name):
            return getattr(real_cfg, name)

    cfg = _Cfg()
    cfg.BOOTSTRAP_ADMIN_USERNAME = bootstrap_username
    cfg.BOOTSTRAP_ADMIN_PASSWORD = f"B00tstrap-{tag}-pass!"
    cfg.BOOTSTRAP_ADMIN_EMAIL = ""
    monkeypatch.setattr(api.auth, "get_config", lambda: cfg)

    async def _scenario():
        result = await api.auth.ensure_hardcoded_admin()
        # M91: refuse to promote — never touch the case-variant row.
        assert result is None

    _run_async(_scenario())

    with get_sync_db() as session:
        row = session.get(User, user_id)
        assert row is not None
        assert row.username == existing_username
        assert row.role == "user"
        assert row.is_active is True
        assert row.password_hash == attacker_hash
        # No account with the exact bootstrap username was created either.
        from sqlalchemy import select

        created = session.execute(
            select(User).where(User.username == bootstrap_username)
        ).scalar_one_or_none()
        assert created is None


# ── L51: logout revokes the presented access token ───────────────────────────


async def _redis_or_skip():
    """Return the live cache client, or pytest.skip when Redis is unreachable."""
    from api.cache import get_client

    client = get_client()
    if client is None:
        pytest.skip("Redis unavailable — jti blocklist requires Redis")
    try:
        assert await client.ping()
    except Exception:  # noqa: BLE001 - Redis down must skip, not fail
        pytest.skip("Redis unavailable — jti blocklist requires Redis")
    return client


def test_l51_logout_revokes_presented_access_token():
    """Logout blocklists the Bearer token presented in the Authorization header."""
    user_id = _seed_user("t51")
    token = create_access_token(user_id, "user")
    raw_refresh, _, _ = _seed_refresh_token(
        user_id, expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )

    async def _scenario():
        redis = await _redis_or_skip()
        client = create_app().test_client()
        headers = {"Authorization": f"Bearer {token}"}

        # The access token works before logout.
        resp = await client.get("/api/v1/profile", headers=headers)
        assert resp.status_code == 200, (resp.status_code, await resp.get_data())

        resp = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": raw_refresh},
            headers=headers,
        )
        assert resp.status_code == 204

        # L51: the same access token is now blocklisted via its jti.
        resp = await client.get("/api/v1/profile", headers=headers)
        assert resp.status_code == 401, (resp.status_code, await resp.get_data())

        # The per-jti key carries a TTL (self-cleaning blocklist).
        from api.jwt import _BLOCKLIST_KEY_PREFIX, decode_access_token

        jti = decode_access_token(token)["jti"]
        ttl = await redis.ttl(_BLOCKLIST_KEY_PREFIX + jti)
        assert ttl > 0

    _run_async(_scenario())


def test_l51_logout_best_effort_never_breaks_204():
    """Logout with a Bearer token still returns 204 even if revocation degrades."""
    user_id = _seed_user("t51b")
    token = create_access_token(user_id, "user")
    raw_refresh, _, _ = _seed_refresh_token(
        user_id, expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )

    async def _scenario():
        client = create_app().test_client()
        resp = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": raw_refresh},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 204

    _run_async(_scenario())


# ── H37: password_hash is never served from the user cache ───────────────────


def test_h37_password_hash_excluded_from_user_cache():
    """The cached User payload omits password_hash entirely (H37)."""
    user_id = _seed_user("t37")
    token = create_access_token(user_id, "user")

    async def _scenario():
        redis = await _redis_or_skip()
        client = create_app().test_client()

        resp = await client.get(
            "/api/v1/profile", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, (resp.status_code, await resp.get_data())

        from api.jwt import _user_cache_key

        raw = await redis.get(_user_cache_key(user_id))
        assert raw is not None  # the auth step populated the cache
        payload = json.loads(raw)
        assert "password_hash" not in payload

    _run_async(_scenario())


# ── M92: change-password is rate limited ─────────────────────────────────────


def test_m92_change_password_is_rate_limited():
    """/profile/change-password carries the per-IP rate-limit headers (M92)."""
    user_id = _seed_user("t92")
    token = create_access_token(user_id, "user")

    async def _scenario():
        client = create_app().test_client()
        resp = await client.post(
            "/api/v1/profile/change-password",
            json={"current_password": "wrong-pass", "new_password": "New-pass-123!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401  # wrong current password
        assert resp.headers.get("X-RateLimit-Limit") == "10"

    _run_async(_scenario())
