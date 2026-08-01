"""
Auth blueprint — registration (auto-login), login, token refresh, and logout.

Every endpoint returns RFC 7807 ``application/problem+json`` on error.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

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
from api.schemas import AuthResponse, LoginRequest, RefreshRequest, RegisterRequest, UserBrief
from config import get_config
from models.base import AsyncSessionLocal
from models.helpers import hash_password
from models.refresh_token import RefreshToken
from models.user import User

logger = logging.getLogger("empyrean.auth")

auth_bp = Blueprint("auth", __name__)

# Precomputed bcrypt hash of a dummy password, compared against when a login
# username does not exist — so unknown usernames take the same time as a wrong
# password and the endpoint does not leak which usernames are registered.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(
    b"timing-equalizer", bcrypt.gensalt(rounds=12)
).decode()


# ── Helpers ────────────────────────────────────────────────────────────────────


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
        cfg = get_config()
        expires_at = now.replace(second=0, microsecond=0) + timedelta(
            days=cfg.JWT_REFRESH_TOKEN_EXPIRY_DAYS
        )

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
async def register():
    """Register a new user and auto-login (return JWT tokens)."""
    body = await request.get_json(silent=True)
    if not body:
        return _problem_json(400, "Bad Request", "Request body is required")

    try:
        data = RegisterRequest(**body)
    except Exception as exc:
        return _problem_json(422, "Unprocessable Entity", str(exc))

    # ── Create user ──────────────────────────────────────────────────────────
    pwd_hash = hash_password(data.password)

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
async def login():
    """Authenticate with username/password, return JWT tokens."""
    body = await request.get_json(silent=True)
    if not body:
        return _problem_json(400, "Bad Request", "Request body is required")

    try:
        data = LoginRequest(**body)
    except Exception as exc:
        return _problem_json(422, "Unprocessable Entity", str(exc))

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.username == data.username)
        )
        user = result.scalar_one_or_none()
        pwd_bytes = data.password.encode("utf-8")

        # Unknown username: burn a bcrypt compare so timing matches a wrong
        # password, and return the same message in every failure case.
        if user is None:
            bcrypt.checkpw(pwd_bytes, _DUMMY_PASSWORD_HASH.encode("utf-8"))
            logger.warning("Failed login: unknown username %r", data.username)
            return _problem_json(401, "Unauthorized", "Invalid username or password")

        if not bcrypt.checkpw(pwd_bytes, user.password_hash.encode("utf-8")):
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
async def refresh():
    """Exchange a valid refresh token for a new JWT pair (token rotation)."""
    body = await request.get_json(silent=True)
    if not body:
        return _problem_json(400, "Bad Request", "Request body is required")

    try:
        data = RefreshRequest(**body)
    except Exception as exc:
        return _problem_json(422, "Unprocessable Entity", str(exc))

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
            )
            .values(revoked=True)
            .returning(RefreshToken.user_id, RefreshToken.expires_at)
        )
        row = result.first()

        # Same 401 whether the token was invalid, already revoked, or expired —
        # don't reveal which.
        if row is None:
            logger.warning("Rejected refresh token: invalid or already revoked")
            return _problem_json(
                401, "Unauthorized", "Refresh token is invalid or expired"
            )
        user_id, expires_at = row
        if expires_at < now:
            logger.warning("Rejected refresh token: expired")
            return _problem_json(
                401, "Unauthorized", "Refresh token is invalid or expired"
            )

        user_result = await session.execute(
            select(User).where(User.id == user_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None or not user.is_active:
            await session.commit()
            return _problem_json(401, "Unauthorized", "User not found or deactivated")

        # Create new refresh token
        raw_new, new_hash = generate_refresh_token()
        cfg = get_config()
        new_expires = now.replace(second=0, microsecond=0) + timedelta(
            days=cfg.JWT_REFRESH_TOKEN_EXPIRY_DAYS
        )

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
async def logout():
    """Revoke a refresh token."""
    body = await request.get_json(silent=True)
    if not body:
        return _problem_json(400, "Bad Request", "Request body is required")

    try:
        data = RefreshRequest(**body)
    except Exception as exc:
        return _problem_json(422, "Unprocessable Entity", str(exc))

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
