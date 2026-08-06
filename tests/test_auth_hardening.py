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
import secrets
from datetime import datetime, timedelta, timezone

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
