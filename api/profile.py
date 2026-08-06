"""
Profile blueprint — manage the currently authenticated user.

All endpoints require ``@jwt_required`` (valid access token).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import bcrypt
from quart import Blueprint, g, jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.jwt import _problem_json, jwt_required
from api.schemas import ChangePasswordRequest, ProfileResponse, UpdateProfileRequest
from models.base import AsyncSessionLocal
from models.helpers import hash_password
from models.user import User

logger = logging.getLogger("empyrean.profile")

profile_bp = Blueprint("profile", __name__)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _serialise_user(user: User) -> dict:
    """Convert a ``User`` ORM instance to a ``ProfileResponse`` dict."""
    return ProfileResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        notification_prefs=user.notification_prefs or {},
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    ).model_dump()


# ── GET /profile ───────────────────────────────────────────────────────────────


@profile_bp.route("", methods=["GET"])
@jwt_required
async def get_profile():
    """Return the current user's profile."""
    user: User = g.current_user
    return jsonify(_serialise_user(user)), 200


# ── PATCH /profile ─────────────────────────────────────────────────────────────


@profile_bp.route("", methods=["PATCH"])
@jwt_required
async def update_profile():
    """Update username, email, or notification preferences."""
    user: User = g.current_user
    body = await request.get_json(silent=True)
    if not body:
        return _problem_json(400, "Bad Request", "Request body is required")

    try:
        data = UpdateProfileRequest(**body)
    except Exception as exc:
        return _problem_json(422, "Unprocessable Entity", str(exc))

    has_changes = False
    if data.username is not None and data.username != user.username:
        user.username = data.username
        has_changes = True
    if data.email is not None and data.email != user.email:
        user.email = data.email
        has_changes = True
    if data.notification_prefs is not None:
        user.notification_prefs = data.notification_prefs
        has_changes = True

    if not has_changes:
        return jsonify(_serialise_user(user)), 200

    async with AsyncSessionLocal() as session:
        # Fetch a session-attached row and re-apply the mutated fields —
        # the detached g.current_user cannot be committed into a fresh session.
        persistent = await session.get(User, user.id)
        if persistent is None:
            return _problem_json(404, "Not Found", "User not found")

        persistent.username = user.username
        persistent.email = user.email
        persistent.notification_prefs = user.notification_prefs

        try:
            await session.commit()
            await session.refresh(persistent)
            g.current_user = persistent
        except IntegrityError:
            await session.rollback()
            return _problem_json(
                409, "Conflict", "Username or email already taken"
            )

    return jsonify(_serialise_user(persistent)), 200


# ── POST /profile/change-password ──────────────────────────────────────────────


@profile_bp.route("/change-password", methods=["POST"])
@jwt_required
async def change_password():
    """Verify current password and set a new one."""
    user: User = g.current_user
    body = await request.get_json(silent=True)
    if not body:
        return _problem_json(400, "Bad Request", "Request body is required")

    try:
        data = ChangePasswordRequest(**body)
    except Exception as exc:
        return _problem_json(422, "Unprocessable Entity", str(exc))

    # bcrypt cost-12 work runs off the event loop so a ~500 ms hash/compare
    # never blocks in-flight requests (H-6).
    current_ok = await asyncio.to_thread(
        bcrypt.checkpw,
        data.current_password.encode("utf-8"),
        user.password_hash.encode("utf-8"),
    )
    if not current_ok:
        return _problem_json(401, "Unauthorized", "Current password is incorrect")

    user.password_hash = await asyncio.to_thread(hash_password, data.new_password)

    async with AsyncSessionLocal() as session:
        session.add(user)
        await session.commit()

    logger.info("Password changed for user %s", user.username)
    return jsonify({"message": "Password changed successfully"}), 200


# ── DELETE /profile ────────────────────────────────────────────────────────────


@profile_bp.route("", methods=["DELETE"])
@jwt_required
async def delete_profile():
    """Delete (soft-deactivate) the current user's account."""
    user: User = g.current_user
    user.is_active = False

    # Revoke all active refresh tokens
    from models.refresh_token import RefreshToken

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id,
                RefreshToken.revoked == False,  # noqa: E712
            )
        )
        for rt in result.scalars().all():
            rt.revoked = True
        session.add(user)
        await session.commit()

    logger.info("Account deactivated for user %s", user.username)
    return jsonify({"message": "Account deleted successfully"}), 200
