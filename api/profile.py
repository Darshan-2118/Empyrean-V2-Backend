"""
Profile blueprint — manage the currently authenticated user.

All endpoints require ``@jwt_required`` (valid access token).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import bcrypt
from quart import Blueprint, g, jsonify
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.jwt import _problem_json, jwt_required
from api.schemas import ChangePasswordRequest, ProfileResponse, UpdateProfileRequest
from api.validation import validate_body, validated_body
from models.base import AsyncSessionLocal
from models.helpers import hash_password
from models.user import User

logger = logging.getLogger("empyrean.profile")

profile_bp = Blueprint("profile", __name__)


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


@profile_bp.route("", methods=["GET"])
@jwt_required
async def get_profile():
    """Return the current user's profile."""
    user: User = g.current_user
    return jsonify(_serialise_user(user)), 200


@profile_bp.route("", methods=["PATCH"])
@jwt_required
@validate_body(UpdateProfileRequest)
async def update_profile():
    """Update username, email, or notification preferences.

    An empty ``{}`` body is a valid no-op and returns the unchanged
    profile with a 200 — only fields actually supplied in the request
    are applied.
    """
    user: User = g.current_user
    data = validated_body()

    # "has_changes" is driven purely by which fields were supplied — not by
    # whether they differ from the current values — so a "{}" body is a valid
    # no-op and only actually-supplied fields are applied below.
    has_changes = any(
        v is not None
        for v in (data.username, data.email, data.notification_prefs)
    )

    if not has_changes:
        return jsonify(_serialise_user(user)), 200

    async with AsyncSessionLocal() as session:
        # Fetch a session-attached row and apply only the supplied fields —
        # the detached g.current_user cannot be committed into a fresh session,
        # and copying every field from the snapshot would clobber a concurrent
        # writer's committed change (lost update).
        persistent = await session.get(User, user.id)
        if persistent is None:
            return _problem_json(404, "Not Found", "User not found")

        if data.username is not None:
            persistent.username = data.username
        if data.email is not None:
            persistent.email = data.email
        if data.notification_prefs is not None:
            persistent.notification_prefs = data.notification_prefs

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


@profile_bp.route("/change-password", methods=["POST"])
@jwt_required
@validate_body(ChangePasswordRequest)
async def change_password():
    """Verify current password and set a new one.

    An empty ``{}`` body fails schema validation (422) — the required
    ``current_password``/``new_password`` fields are missing.
    """
    user: User = g.current_user
    data = validated_body()

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

    # A changed password must kill every outstanding refresh token so a prior
    # session can't outlive the new credentials.
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

    logger.info("Password changed for user %s", user.username)
    return jsonify({"message": "Password changed successfully"}), 200


@profile_bp.route("", methods=["DELETE"])
@jwt_required
async def delete_profile():
    """Delete (soft-deactivate) the current user's account."""
    user: User = g.current_user
    user.is_active = False

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
