"""
Auth blueprint — registration (auto-login), login, token refresh, and logout.

Every endpoint returns RFC 7807 ``application/problem+json`` on error.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from quart import Blueprint, jsonify, request
from sqlalchemy import select, update as sa_update
from sqlalchemy.exc import IntegrityError

from api.jwt import (
    _problem_json,
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from api.rate_limit import rate_limit
from api.schemas import AuthResponse, LoginRequest, RefreshRequest, RegisterRequest, UserBrief
from config import get_config
from models.base import AsyncSessionLocal
from models.helpers import hash_password
from models.refresh_token import RefreshToken
from models.user import User

logger = logging.getLogger("empyrean.auth")

auth_bp = Blueprint("auth", __name__)

# bcrypt hash of a dummy password, compared against when a login username does
# not exist — so unknown usernames take the same time as a wrong password and
# the endpoint does not leak which usernames are registered. Computed lazily on
# first failed-login use, not at module import, so a process import never pays
# a full cost-12 hash (~500 ms) (L-33).
_DUMMY_PASSWORD_HASH: str | None = None


def _dummy_password_hash() -> str:
    """Return the dummy bcrypt hash, computing it on first use (L-33)."""
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = bcrypt.hashpw(
            b"timing-equalizer", bcrypt.gensalt(rounds=12)
        ).decode()
    return _DUMMY_PASSWORD_HASH


def _dummy_compare(pwd_bytes: bytes) -> None:
    """Timing-equalizing bcrypt compare against the lazy dummy hash (H-6/L-33).

    The handler awaits this via ``asyncio.to_thread``, so both the one-time
    cost-12 hash computation (on first unknown-username login) and the check
    against it run off the Quart event loop. The result is discarded — only
    the timing must match a real password check.
    """
    bcrypt.checkpw(pwd_bytes, _dummy_password_hash().encode("utf-8"))


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _json_body(model: type[Any]) -> tuple[Any | None, Any | None]:
    """Parse the request body into ``model``, returning ``(data, error_response)``.

    ``error_response`` is a ready-to-return RFC 7807 ``(body, status, headers)``
    tuple when the body is missing or fails validation, else ``None``.

    Distinction: ``request.get_json(silent=True)`` returning ``None`` (missing
    body OR malformed JSON) -> 400; any non-``None`` value, including an empty
    ``{}`` object, falls through to schema validation -> 422 on failure.
    """
    body = await request.get_json(silent=True)
    if body is None:
        return None, _problem_json(400, "Bad Request", "Request body is required")
    try:
        return model(**body), None
    except Exception as exc:
        return None, _problem_json(422, "Unprocessable Entity", str(exc))


def _refresh_expiry(now: datetime, cfg: Any) -> datetime:
    """Refresh-token expiry time for ``now``, truncated to the second."""
    return now.replace(second=0, microsecond=0) + timedelta(
        days=cfg.JWT_REFRESH_TOKEN_EXPIRY_DAYS
    )


def _auth_payload(user: User, access_token: str, refresh_token: str) -> dict:
    """Build the ``AuthResponse`` payload dict for a token pair."""
    cfg = get_config()
    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=cfg.JWT_ACCESS_TOKEN_EXPIRY_MINUTES * 60,
        role=user.role,
        user=UserBrief(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
        ),
    ).model_dump()


async def _issue_auth_tokens(user: User) -> tuple:
    """Create a JWT pair, persist the refresh token, return a 201 response.

    Shared by register and login.
    """
    access = create_access_token(user.id, user.role)
    raw_refresh, token_hash = generate_refresh_token()

    async with AsyncSessionLocal() as session:
        now = datetime.now(timezone.utc)
        expires_at = _refresh_expiry(now, get_config())

        rt = RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        session.add(rt)
        await session.commit()

    return jsonify(_auth_payload(user, access, raw_refresh)), 201


# ── POST /auth/register ────────────────────────────────────────────────────────


@auth_bp.route("/register", methods=["POST"])
@rate_limit(5, 60)  # M-12: stricter per-IP cap — account creation is a spam vector
async def register():
    """Register a new user and auto-login (return JWT tokens)."""
    data, err = await _json_body(RegisterRequest)
    if err is not None:
        return err

    # ── Create user ──────────────────────────────────────────────────────────
    # bcrypt cost-12 hashing is ~500 ms — run it off the event loop (H-6).
    pwd_hash = await asyncio.to_thread(hash_password, data.password)

    async with AsyncSessionLocal() as session:
        user = User(
            username=data.username,
            email=data.email,
            password_hash=pwd_hash,
            role="user",
            is_active=True,
            notification_prefs={},
        )
        session.add(user)
        try:
            await session.commit()
            await session.refresh(user)
        except IntegrityError:
            await session.rollback()
            return _problem_json(
                409,
                "Conflict",
                "Username or email already taken",
            )

    # ── Auto-login ───────────────────────────────────────────────────────────
    return await _issue_auth_tokens(user)


# ── POST /auth/login ───────────────────────────────────────────────────────────


@auth_bp.route("/login", methods=["POST"])
@rate_limit(10, 60)  # M-12: brute-force throttle (10/min per IP)
async def login():
    """Authenticate with username/password, return JWT tokens."""
    data, err = await _json_body(LoginRequest)
    if err is not None:
        return err

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.username == data.username)
        )
        user = result.scalar_one_or_none()
        pwd_bytes = data.password.encode("utf-8")

        if user is None:
            # Unknown username: burn a bcrypt compare so timing matches a wrong
            # password, and return the same message in every failure case. The
            # dummy hash is computed (on first use) and compared entirely off
            # the event loop (H-6/L-33).
            await asyncio.to_thread(_dummy_compare, pwd_bytes)
            logger.warning("Failed login: unknown username %r", data.username)
            return _problem_json(401, "Unauthorized", "Invalid username or password")

        if not await asyncio.to_thread(
            bcrypt.checkpw, pwd_bytes, user.password_hash.encode("utf-8")
        ):
            logger.warning("Failed login: wrong password for %r", data.username)
            return _problem_json(401, "Unauthorized", "Invalid username or password")

        if not user.is_active:
            logger.warning("Failed login: inactive user %r", data.username)
            return _problem_json(401, "Unauthorized", "Invalid username or password")

        # Update last_login_at
        user.last_login_at = datetime.now(timezone.utc)
        await session.commit()

    return await _issue_auth_tokens(user)


# ── POST /auth/refresh ─────────────────────────────────────────────────────────


@auth_bp.route("/refresh", methods=["POST"])
@rate_limit(10, 60)  # M-12: per-IP cap on token rotation (brute-force surface)
async def refresh():
    """Exchange a valid refresh token for a new JWT pair (token rotation)."""
    data, err = await _json_body(RefreshRequest)
    if err is not None:
        return err

    token_hash = hash_refresh_token(data.refresh_token)
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        # Claim the token atomically: mark it revoked and read the owner in one
        # UPDATE ... RETURNING, so two concurrent requests with the same token
        # cannot both rotate it into two live refresh tokens.
        result = await session.execute(
            sa_update(RefreshToken)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked == False,  # noqa: E712
                # Exclude expired tokens so the UPDATE never claims one (L-27)
                # — an expired token falls through to ``row is None`` below
                # (generic 401, no mutation, no forever-rotation hole).
                RefreshToken.expires_at > now,
            )
            .values(revoked=True)
            .returning(RefreshToken.user_id, RefreshToken.expires_at)
        )
        row = result.first()

        # Same 401 whether the token was invalid, already revoked, or expired —
        # don't reveal which.
        if row is None:
            logger.warning("Rejected refresh token: invalid, already revoked, or expired")
            return _problem_json(
                401, "Unauthorized", "Refresh token is invalid or expired"
            )
        user_id, _ = row

        user_result = await session.execute(
            select(User).where(User.id == user_id)
        )
        user = user_result.scalar_one_or_none()
        # Same generic 401 detail as the invalid/expired-token branches — do not
        # reveal whether an account is missing or deactivated (L-26).
        if user is None or not user.is_active:
            await session.commit()
            return _problem_json(
                401, "Unauthorized", "Refresh token is invalid or expired"
            )

        # Create new refresh token
        raw_new, new_hash = generate_refresh_token()
        new_expires = _refresh_expiry(now, get_config())

        new_rt = RefreshToken(
            user_id=user.id,
            token_hash=new_hash,
            expires_at=new_expires,
        )
        session.add(new_rt)
        await session.commit()

    # Build response
    access = create_access_token(user.id, user.role)
    return jsonify(_auth_payload(user, access, raw_new)), 200


# ── POST /auth/logout ──────────────────────────────────────────────────────────


@auth_bp.route("/logout", methods=["POST"])
@rate_limit(10, 60)  # M-12: per-IP cap on token revocation (write-flood surface)
async def logout():
    """Revoke a refresh token."""
    data, err = await _json_body(RefreshRequest)
    if err is not None:
        return err

    token_hash = hash_refresh_token(data.refresh_token)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked == False,  # noqa: E712
            )
        )
        rt = result.scalar_one_or_none()

        if rt is not None:
            rt.revoked = True
            await session.commit()

    # Always return 204 — don't reveal whether the token existed
    return "", 204
