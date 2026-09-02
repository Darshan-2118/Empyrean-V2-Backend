"""
Tests for the forgot / reset password flow.

These exercise the real Quart app over HTTP (``app.test_client()``) against the
``empyrean_test`` DB, mirroring ``test_api.py``'s style. The email transport is
best-effort and fail-soft, so the tests mock ``api.auth._send_password_reset_email``
to capture the reset URL (and thus the raw token) that the endpoint would have
emailed — no SMTP is touched.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app import create_app
from models import PasswordResetToken, RefreshToken, User
from models.base import AsyncSessionLocal, dispose_engines
from models.helpers import hash_password
from api.auth import hash_password_reset_token, make_password_reset_token

API = "/api/v1"

_CREATED_USERNAMES: set[str] = set()


@pytest.fixture(autouse=True)
def _fast_redis_down(monkeypatch):
    """Simulate an unreachable Redis fast (documented fail-open path)."""
    monkeypatch.setattr("api.rate_limit.get_client", lambda: None)
    monkeypatch.setattr("api.cache.get_client", lambda: None)


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _run(coro):
    """Run a scenario in a fresh loop, disposing DB engines on the way out."""

    async def _runner():
        try:
            await coro
        finally:
            try:
                await _cleanup_tracked_rows()
            finally:
                await dispose_engines()

    return asyncio.run(_runner())


async def _cleanup_tracked_rows() -> None:
    async with AsyncSessionLocal() as session:
        if _CREATED_USERNAMES:
            await session.execute(
                delete(User).where(User.username.in_(_CREATED_USERNAMES))
            )
        await session.commit()
    _CREATED_USERNAMES.clear()


async def _create_user(username: str, password: str = "test-pass-1") -> User:
    """Register a user directly so we know their email + password."""
    _CREATED_USERNAMES.add(username)
    async with AsyncSessionLocal() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password(password, rounds=4),
            role="user",
            is_active=True,
            notification_prefs={},
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _capture_reset_url(captured: list[str]):
    """Return a mock for ``_send_password_reset_email`` that records the URL.

    ``_send_password_reset_email(email, reset_url)`` is called with the reset
    URL (``...?token=<raw>``) as its second arg; capture it so tests can read
    the raw token back and drive ``reset-password`` as a real client would.
    """

    def _mock(email: str, reset_url: str) -> None:
        captured.append(reset_url)

    return _mock


# ── Token helpers & schema ─────────────────────────────────────────────────────


def test_make_password_reset_token_unique_and_hashed_only():
    """Raw + digest round-trip; two calls yield distinct raw tokens."""
    raw1, h1 = make_password_reset_token()
    raw2, h2 = make_password_reset_token()
    assert raw1 and h1 and raw2 and h2
    assert raw1 != raw2
    assert h1 != h2
    assert hash_password_reset_token(raw1) == h1
    assert len(h1) == 64  # SHA-256 hex


def test_reset_password_schema_rejects_short_password():
    """A <6-char new password is a 422 (schema), not an action."""
    from api.schemas import ResetPasswordRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResetPasswordRequest(token="x" * 16, new_password="short")


# ── Forgot password ────────────────────────────────────────────────────────────


def test_forgot_password_does_not_leak_account_existence(monkeypatch):
    """Both a registered and an unregistered email get the identical generic 202."""

    async def scenario():
        app = create_app()
        username = _unique("leak")
        await _create_user(username)
        captured: list[str] = []
        monkeypatch.setattr("api.auth._send_password_reset_email", _capture_reset_url(captured))

        async with app.test_client() as client:
            real = await client.post(
                f"{API}/auth/forgot-password",
                json={"email": f"{username}@example.com"},
            )
            ghost = await client.post(
                f"{API}/auth/forgot-password",
                json={"email": "nobody@example.com"},
            )
            assert real.status_code == 202
            assert ghost.status_code == 202
            real_body = await real.get_json()
            ghost_body = await ghost.get_json()
            assert real_body == ghost_body
            assert "reset" in real_body["message"].lower()
            assert len(captured) == 1  # only the real user got an email

    _run(scenario())


def test_forgot_password_issues_token_and_invalidates_prior(monkeypatch):
    """A second request supersedes the first (older token marked used)."""

    async def scenario():
        app = create_app()
        username = _unique("supersede")
        user = await _create_user(username)
        captured: list[str] = []
        monkeypatch.setattr("api.auth._send_password_reset_email", _capture_reset_url(captured))

        async with app.test_client() as client:
            await client.post(
                f"{API}/auth/forgot-password", json={"email": f"{username}@example.com"}
            )
            first_url = captured[-1]
            first_token = first_url.split("token=")[1]
            await client.post(
                f"{API}/auth/forgot-password", json={"email": f"{username}@example.com"}
            )
            second_url = captured[-1]
            second_token = second_url.split("token=")[1]
            assert first_token != second_token

        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(PasswordResetToken).where(
                        PasswordResetToken.user_id == user.id
                    )
                )
            ).scalars().all()
            # Two rows exist: the first used, the second still pending.
            assert len(rows) == 2
            by_hash = {r.token_hash: r for r in rows}
            first = by_hash[hash_password_reset_token(first_token)]
            second = by_hash[hash_password_reset_token(second_token)]
            assert first.used_at is not None
            assert second.used_at is None

    _run(scenario())


# ── Reset password ─────────────────────────────────────────────────────────────


def test_reset_password_success_and_token_one_time(monkeypatch):
    """A valid token changes the password; replaying it 401s."""

    async def scenario():
        app = create_app()
        username = _unique("reset")
        user = await _create_user(username, password="original-pass")
        captured: list[str] = []
        monkeypatch.setattr("api.auth._send_password_reset_email", _capture_reset_url(captured))

        async with app.test_client() as client:
            await client.post(
                f"{API}/auth/forgot-password", json={"email": f"{username}@example.com"}
            )
            raw = captured[-1].split("token=")[1]

            # Bad token → 401 generic
            bad = await client.post(
                f"{API}/auth/reset-password",
                json={"token": "deadbeef" * 8, "new_password": "new-pass-123"},
            )
            assert bad.status_code == 401

            # Good token → 200
            ok = await client.post(
                f"{API}/auth/reset-password",
                json={"token": raw, "new_password": "new-pass-123"},
            )
            assert ok.status_code == 200
            assert (await ok.get_json())["message"] == "Password reset successfully"

        # The reset revoked every token outstanding at reset time. (No login has
        # happened yet, so none should remain.) The `created_at` sweep below is
        # separate; here we assert the revocation effect directly.
        async with AsyncSessionLocal() as session:
            alive = (
                await session.execute(
                    select(RefreshToken).where(
                        RefreshToken.user_id == user.id,
                        RefreshToken.revoked == False,  # noqa: E712
                    )
                )
            ).scalars().all()
            assert not alive

        async with app.test_client() as client:
            # Old password no longer works, new one does.
            old_login = await client.post(
                f"{API}/auth/login", json={"username": username, "password": "original-pass"}
            )
            new_login = await client.post(
                f"{API}/auth/login", json={"username": username, "password": "new-pass-123"}
            )
            assert old_login.status_code == 401
            assert new_login.status_code == 201

            # Replaying the same token → 401 (one-time).
            replay = await client.post(
                f"{API}/auth/reset-password",
                json={"token": raw, "new_password": "another-pass"},
            )
            assert replay.status_code == 401

        # The post-reset login issued a brand-new (unrevoked) refresh token.
        async with AsyncSessionLocal() as session:
            alive = (
                await session.execute(
                    select(RefreshToken).where(
                        RefreshToken.user_id == user.id,
                        RefreshToken.revoked == False,  # noqa: E712
                    )
                )
            ).scalars().all()
            assert len(alive) == 1

    _run(scenario())


def test_reset_password_rejects_expired_token(monkeypatch):
    """A token past its expiry cannot be redeemed."""

    async def scenario():
        app = create_app()
        username = _unique("expired")
        user = await _create_user(username, password="original-pass")
        raw, token_hash = make_password_reset_token()
        # Insert a token that is already expired.
        async with AsyncSessionLocal() as session:
            session.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=token_hash,
                    expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                )
            )
            await session.commit()

        app = create_app()
        async with app.test_client() as client:
            resp = await client.post(
                f"{API}/auth/reset-password",
                json={"token": raw, "new_password": "new-pass-123"},
            )
            assert resp.status_code == 401
            assert (await resp.get_json())["status"] == 401

    _run(scenario())


def test_reset_password_validates_schema():
    """Missing/short new_password or token is a 422."""

    async def scenario():
        app = create_app()
        async with app.test_client() as client:
            bad_pw = await client.post(
                f"{API}/auth/reset-password", json={"token": "x" * 16, "new_password": "short"}
            )
            bad_tok = await client.post(
                f"{API}/auth/reset-password", json={"token": "", "new_password": "new-pass-123"}
            )
            assert bad_pw.status_code == 422
            assert bad_tok.status_code == 422

    _run(scenario())


# ── Cleanup task ───────────────────────────────────────────────────────────────


def test_password_reset_token_cleanup():
    """The daily sweep removes used and expired tokens, keeps live ones."""
    from tasks.aggregation import password_reset_token_cleanup

    async def scenario():
        user = await _create_user(_unique("cleanup"))
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            session.add_all(
                [
                    PasswordResetToken(
                        user_id=user.id,
                        token_hash=hash_password_reset_token("used" + "x" * 16),
                        expires_at=now + timedelta(minutes=60),
                        used_at=now,
                    ),
                    PasswordResetToken(
                        user_id=user.id,
                        token_hash=hash_password_reset_token("expired" + "x" * 16),
                        expires_at=now - timedelta(minutes=5),
                    ),
                    PasswordResetToken(
                        user_id=user.id,
                        token_hash=hash_password_reset_token("live" + "x" * 16),
                        expires_at=now + timedelta(minutes=60),
                    ),
                ]
            )
            await session.commit()
        # Reconfirm the raw tokens map to the intended hashes.
        used_hash = hash_password_reset_token("used" + "x" * 16)
        expired_hash = hash_password_reset_token("expired" + "x" * 16)
        live_hash = hash_password_reset_token("live" + "x" * 16)

        result = password_reset_token_cleanup()

        assert result["deleted"] == 2
        async with AsyncSessionLocal() as session:
            hashes = (
                (await session.execute(select(PasswordResetToken.token_hash)))
            ).scalars().all()
        assert live_hash in hashes
        assert used_hash not in hashes
        assert expired_hash not in hashes

    _run(scenario())
