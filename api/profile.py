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

from api.jwt import problem_json, jwt_required
from api.schemas import ChangePasswordRequest, ProfileResponse, UpdateProfileRequest
from api.validation import validate_body, validated_body
from models.base import AsyncSessionLocal
from models.helpers import hash_password
from models.refresh_token import RefreshToken
from models.user import User

logger = logging.getLogger("empyrean.profile")

profile_bp = Blueprint("profile", __name__)


def _serialise_user(user: User) -> dict:
    """Convert a ``User`` ORM instance to a ``ProfileResponse`` dict.

    M32: the dump is cached on ``g`` per request — profile endpoints are
    high-frequency and a handler that serialises the same user object twice
    (e.g. the no-change and post-commit paths) must not re-run the full
    pydantic dump. Keyed by object identity so a *different* refreshed
    instance is always re-serialised.
    """
    cached = getattr(g, "_profile_payload", None)
    if cached is not None and cached[0] is user:
        return cached[1]
    payload = ProfileResponse(
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
    g._profile_payload = (user, payload)
    return payload


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

    # H17: pydantic v2 records exactly which fields the client supplied, so an
    # explicit ``"notification_prefs": null`` can now be distinguished from an
    # absent key — null *clears* the preferences back to {}.
    supplied_fields = data.model_fields_set
    clear_prefs = "notification_prefs" in supplied_fields and (
        data.notification_prefs is None
    )

    # "has_changes" is driven purely by which fields were supplied — not by
    # whether they differ from the current values — so a "{}" body is a valid
    # no-op and only actually-supplied fields are applied below.
    has_changes = clear_prefs or any(
        v is not None
        for v in (data.username, data.email, data.notification_prefs)
    )

    if not has_changes:
        return jsonify(_serialise_user(user)), 200

    # M86: all field validation lives in the schemas (UpdateProfileRequest /
    # NotificationPrefs) and now matches registration exactly — ASCII-only
    # usernames via the shared _normalise_username validator, EmailStr with
    # the same 255-char cap, typed prefs with the same bounds. The old
    # hand-rolled block here diverged (Unicode usernames slipped past
    # .isalnum(), caps differed) and is removed.

    async with AsyncSessionLocal() as session:
        # Fetch a session-attached row and apply only the supplied fields —
        # the detached g.current_user cannot be committed into a fresh session,
        # and copying every field from the snapshot would clobber a concurrent
        # writer's committed change (lost update).
        persistent = await session.get(User, user.id)
        if persistent is None:
            return problem_json(404, "Not Found", "User not found")

        if data.username is not None:
            persistent.username = data.username
        if data.email is not None:
            persistent.email = data.email
        if clear_prefs:
            persistent.notification_prefs = {}
        elif data.notification_prefs is not None:
            # H13: dump the validated NotificationPrefs model to a plain JSON-
            # safe dict (mode="json" stringifies HttpUrl objects) for JSONB.
            persistent.notification_prefs = data.notification_prefs.model_dump(
                mode="json", exclude_none=True
            )

        try:
            await session.commit()
            await session.refresh(persistent)
            g.current_user = persistent
        except IntegrityError:
            await session.rollback()
            return problem_json(
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
        return problem_json(401, "Unauthorized", "Current password is incorrect")

    # M85: the old code hashed the new password here onto the *detached*
    # g.current_user — a dead write that was discarded (and cost a second
    # ~500 ms bcrypt round). The only real write is the one onto the
    # session-attached row below.

    # A changed password must kill every outstanding refresh token so a prior
    # session can't outlive the new credentials.
    from models.refresh_token import RefreshToken

    async with AsyncSessionLocal() as session:
        # Re-fetch user within this session to avoid detached instance error
        persistent = await session.get(User, user.id)
        if persistent is None:
            return problem_json(404, "Not Found", "User not found")

        persistent.password_hash = await asyncio.to_thread(hash_password, data.new_password)

        result = await session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id,
                RefreshToken.revoked == False,  # noqa: E712
            )
        )
        for rt in result.scalars().all():
            rt.revoked = True
        await session.commit()

    # H16: also blocklist the *current* access token so the session that just
    # changed the password cannot keep using it for up to 15 minutes.
    from api.jwt import decode_access_token, revoke_access_token

    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        try:
            payload = decode_access_token(auth_header[7:].strip())
            await revoke_access_token(payload)
        except Exception:  # noqa: BLE001 — revocation is best-effort
            logger.warning("Could not revoke current access token after password change")

    logger.info("Password changed for user %s", user.username)
    return jsonify({"message": "Password changed successfully"}), 200


@profile_bp.route("", methods=["DELETE"])
@jwt_required
async def delete_profile():
    """Deactivate the current user's account (soft delete).

    H15: this is deliberately **not** a hard delete — the row (and reading
    history) are retained, refresh tokens are revoked, and access-token
    revocation kills live sessions. The response says "deactivated" so users
    are never misled into believing their data was erased. Long-deactivated
    accounts have their PII anonymised by ``data_retention_cleanup``
    (tasks/aggregation.py) once they age past the retention window.
    """
    user: User = g.current_user

    async with AsyncSessionLocal() as session:
        # Re-fetch user within this session to avoid detached instance error
        persistent = await session.get(User, user.id)
        if persistent is None:
            return problem_json(404, "Not Found", "User not found")

        persistent.is_active = False

        result = await session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id,
                RefreshToken.revoked == False,  # noqa: E712
            )
        )
        for rt in result.scalars().all():
            rt.revoked = True
        await session.commit()

        # M12: evict the cached row so deactivation is effective immediately.
        from api.jwt import invalidate_user_cache

        await invalidate_user_cache(user.id)

    logger.info("Account deactivated for user %s", user.username)
    return jsonify({"message": "Account deactivated successfully"}), 200
