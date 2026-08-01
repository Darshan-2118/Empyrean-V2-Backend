"""
JWT encoding, decoding, and route-protection decorators.

Two decorators are exposed:

* ``@jwt_required`` — valid JWT must be present; the :class:`User` object
  is attached to ``g.current_user``.
* ``@admin_required`` — same, but also checks ``role == 'admin'``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable

import jwt as pyjwt
from quart import g, jsonify, request

from config import get_config

logger = logging.getLogger("empyrean.auth")

cfg = get_config()
JWT_SECRET = cfg.JWT_SECRET
JWT_ALGORITHM = cfg.JWT_ALGORITHM
ACCESS_TOKEN_EXPIRY_MINUTES = cfg.JWT_ACCESS_TOKEN_EXPIRY_MINUTES
REFRESH_TOKEN_EXPIRY_DAYS = cfg.JWT_REFRESH_TOKEN_EXPIRY_DAYS


# ── Helpers ────────────────────────────────────────────────────────────────────


def _problem_json(status: int, title: str, detail: str | None = None):
    """Return an RFC 7807 ``application/problem+json`` response."""
    return jsonify(
        {
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail or title,
        }
    ), status, {"Content-Type": "application/problem+json"}


# ── Access token (JWT) ─────────────────────────────────────────────────────────


def create_access_token(user_id: int, role: str) -> str:
    """Create a short-lived JWT access token (HS256)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRY_MINUTES),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an access token.

    Returns the decoded payload on success.
    Raises ``jwt.ExpiredSignatureError`` or ``jwt.InvalidTokenError`` on failure.
    """
    payload = pyjwt.decode(
        token,
        JWT_SECRET,
        algorithms=[JWT_ALGORITHM],
        options={"require": ["sub", "exp"]},
    )
    if payload.get("type") != "access":
        raise pyjwt.InvalidTokenError("Not an access token")
    return payload


# ── Refresh token (opaque, DB-tracked) ────────────────────────────────────────


def generate_refresh_token() -> tuple[str, str]:
    """Return ``(raw_token, sha256_hash)``.

    The raw token is returned to the client; the hash is stored in the DB.
    """
    raw = secrets.token_urlsafe(64)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


def hash_refresh_token(raw_token: str) -> str:
    """Hash a raw refresh token for DB lookup."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


# ── Decorators ─────────────────────────────────────────────────────────────────


def jwt_required(f: Callable) -> Callable:
    """Require a valid JWT access token.

    Attaches the ``User`` instance to ``g.current_user``.
    """

    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> Any:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _problem_json(
                401, "Unauthorized", "Missing or malformed Authorization header"
            )

        token = auth.removeprefix("Bearer ").strip()
        try:
            payload = decode_access_token(token)
        except pyjwt.ExpiredSignatureError:
            return _problem_json(401, "Token expired", "Access token has expired")
        except pyjwt.InvalidTokenError:
            return _problem_json(401, "Invalid token", "Access token is not valid")

        user_id: int = payload.get("sub")

        from models.base import AsyncSessionLocal
        from models.user import User

        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)
            if user is None or not user.is_active:
                return _problem_json(
                    401, "Unauthorized", "User not found or deactivated"
                )
            g.current_user = user

        return await f(*args, **kwargs)

    return decorated


def admin_required(f: Callable) -> Callable:
    """Require a valid JWT with ``role == 'admin'``.

    Reads ``g.current_user`` (populated by ``@jwt_required``). This is
    independent of decorator stacking order: an unauthenticated request
    gets 401, a non-admin user gets 403.
    """

    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> Any:
        user = getattr(g, "current_user", None)
        if user is None:
            return _problem_json(401, "Unauthorized", "Authentication is required")
        if user.role != "admin":
            return _problem_json(403, "Forbidden", "Admin privileges are required")
        return await f(*args, **kwargs)

    return decorated
