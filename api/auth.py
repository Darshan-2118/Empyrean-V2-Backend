"""
Auth blueprint — registration (auto-login), login, token refresh, and logout.

Every endpoint returns RFC 7807 ``application/problem+json`` on error.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import bcrypt
from quart import Blueprint, jsonify, request
from sqlalchemy import select
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


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _build_auth_response(user: User) -> tuple:
    """Create JWT pair, persist refresh token, return ``AuthResponse`` JSON.

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

    expires_in = cfg.JWT_ACCESS_TOKEN_EXPIRY_MINUTES * 60

    resp = AuthResponse(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=expires_in,
        role=user.role,
        user=UserBrief(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
        ),
    ).model_dump()

    return jsonify(resp), 201


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
    return await _build_auth_response(user)


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

        if user is None:
            return _problem_json(401, "Unauthorized", "Invalid username or password")

        if not user.is_active:
            return _problem_json(401, "Unauthorized", "Account is deactivated")

        if not bcrypt.checkpw(
            data.password.encode("utf-8"),
            user.password_hash.encode("utf-8"),
        ):
            return _problem_json(401, "Unauthorized", "Invalid username or password")

        # Update last_login_at
        user.last_login_at = datetime.now(timezone.utc)
        await session.commit()

    return await _build_auth_response(user)


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
        result = await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked == False,  # noqa: E712
            )
        )
        rt = result.scalar_one_or_none()

        if rt is None:
            return _problem_json(401, "Unauthorized", "Refresh token is invalid or revoked")

        if rt.expires_at < now:
            return _problem_json(401, "Token expired", "Refresh token has expired")

        # ── Rotate: revoke old, issue new ────────────────────────────────────
        rt.revoked = True

        user_result = await session.execute(
            select(User).where(User.id == rt.user_id)
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
    expires_in = cfg.JWT_ACCESS_TOKEN_EXPIRY_MINUTES * 60

    resp = AuthResponse(
        access_token=access,
        refresh_token=raw_new,
        expires_in=expires_in,
        role=user.role,
        user=UserBrief(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
        ),
    ).model_dump()

    return jsonify(resp), 200


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
