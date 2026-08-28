"""
JWT encoding, decoding, and route-protection decorators.

Two decorators are exposed:

* ``@jwt_required`` — valid JWT must be present; the :class:`User` object
  is attached to ``g.current_user``.
* ``@admin_required`` — same, but also checks ``role == 'admin'``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable

import jwt as pyjwt
from quart import g, jsonify, request

from config import get_config

logger = logging.getLogger("empyrean.auth")

# H4/M66: the algorithm is pinned literally, never read from config. Even
# though Config now validates JWT_ALGORITHM == "HS256", defense in depth says
# the security-critical decode path must not depend on a mutable knob at all.
JWT_ALGORITHM = "HS256"

# L52: JWT_SECRET and the access-token expiry are resolved via get_config()
# inside each call (no module-level snapshot), so a reset_config_cache() is
# never left serving a stale secret/expiry.

# M12: short-TTL Redis cache for the authenticated User row, so most requests
# avoid a DB round-trip. ~30s TTL bounds deactivation/role-change staleness;
# callers invalidate the key on any is_active/role mutation.
_USER_CACHE_KEY = "cache:user:{user_id}"
_USER_CACHE_TTL_S = 30


def _user_cache_key(user_id: int) -> str:
    return _USER_CACHE_KEY.format(user_id=user_id)


def _serialise_user_for_cache(user) -> dict:
    """Return the JSON-safe dict of a User row, or ``None`` if it can't be built."""
    prefs = user.notification_prefs or {}
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        # H37: password_hash is deliberately never cached — a stale cached hash
        # would let the old password validate after a change; consumers must
        # fetch the hash fresh from the DB.
        "role": user.role,
        "is_active": bool(user.is_active),
        "notification_prefs": prefs,
        "last_login_at": _iso(user.last_login_at),
        "created_at": _iso(user.created_at),
        "updated_at": _iso(user.updated_at),
    }


def _iso(dt) -> str | None:
    """Render a datetime as ISO8601 (or None) for the cache."""
    if dt is None:
        return None
    return dt.isoformat()


def _cache_hit_to_user(payload: dict):
    """Reconstruct a detached User instance from a cached payload.

    Datetime fields are strings in the cache and must be re-parsed so the
    ORM model receives proper ``datetime`` values.
    """
    from models.user import User

    def _parse(v):
        if v is None:
            return None
        try:
            return datetime.fromisoformat(v)
        except (TypeError, ValueError):
            return None

    # H37: password_hash is never cached, so the reconstructed user carries no
    # hash (legacy payloads that still contain the key are tolerated/ignored) —
    # any password verification must read the current hash from the DB.
    return User(
        id=payload["id"],
        username=payload["username"],
        email=payload["email"],
        role=payload["role"],
        is_active=payload["is_active"],
        notification_prefs=payload.get("notification_prefs") or {},
        last_login_at=_parse(payload.get("last_login_at")),
        created_at=_parse(payload.get("created_at")),
        updated_at=_parse(payload.get("updated_at")),
    )


async def invalidate_user_cache(user_id: int) -> None:
    """Evict the cached User row for ``user_id`` (M12).

    Call after any ``is_active``/role mutation so the 30s TTL can't serve a
    stale record. Best-effort — never raises.
    """
    try:
        from api.cache import get_client

        client = get_client()
        if client is None:
            return
        await client.delete(_user_cache_key(user_id))
    except Exception:
        logger.exception("Failed to invalidate user cache for id %d", user_id)


async def _get_user_cached(user_id: int):
    """Return the User for ``user_id``, via the Redis cache when possible.

    On a hit, returns a detached :class:`User` reconstructed from the cache
    (M12). On a miss it loads from the DB and populates the cache. Fails open
    to the DB if Redis is unavailable.
    """
    from models.user import User

    try:
        from api.cache import get_client

        client = get_client()
        if client is not None:
            raw = await client.get(_user_cache_key(user_id))
            if raw:
                payload = json.loads(raw)
                if payload.get("id") == user_id:
                    return _cache_hit_to_user(payload)
    except Exception:
        logger.exception("User-cache read failed for id %d — falling back to DB", user_id)

    from models.base import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user is None:
            return None

    try:
        from api.cache import get_client

        client = get_client()
        if client is not None:
            await client.setex(
                _user_cache_key(user_id),
                _USER_CACHE_TTL_S,
                json.dumps(_serialise_user_for_cache(user)),
            )
    except Exception:
        logger.exception("User-cache write failed for id %d", user_id)
    return user


def problem_json(status: int, title: str, detail: str | None = None):
    """Return an RFC 7807 ``application/problem+json`` response.

    L3: public (no leading underscore) — it is imported across the API
    package, so it is part of the module's public surface.
    """
    return jsonify(
        {
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail or title,
        }
    ), status, {"Content-Type": "application/problem+json"}


def create_access_token(user_id: int, role: str) -> str:
    """Create a short-lived JWT access token (HS256).

    The token carries a unique ``jti`` claim so it can be individually
    revoked via the per-jti Redis blocklist (H7/L51) — on logout or password
    change.
    """
    cfg = get_config()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "jti": secrets.token_urlsafe(16),
        "iat": now,
        "exp": now + timedelta(minutes=cfg.JWT_ACCESS_TOKEN_EXPIRY_MINUTES),
    }
    return pyjwt.encode(payload, cfg.JWT_SECRET, algorithm=JWT_ALGORITHM)


# ── Access-token revocation (H7/H16) ──────────────────────────────────────────
# L51: one Redis key per revoked ``jti`` (``jwt:blocklist:{jti}``), written
# atomically with ``SET ... EX <ttl>`` where the TTL is the token's remaining
# lifetime — every entry expires itself, so the blocklist self-cleans and can
# never grow unboundedly.
# When Redis is unavailable the check fails *open* (matching the rate limiter's
# documented posture): a 15-minute access token is still bounded by its expiry,
# and revocation is best-effort rather than a hard dependency.

_BLOCKLIST_KEY_PREFIX = "jwt:blocklist:"


async def revoke_access_token(payload: dict[str, Any]) -> None:
    """Add a decoded access-token payload's ``jti`` to the revocation blocklist.

    Safe to call with any payload shape; tokens without a ``jti`` (legacy) are
    silently skipped. Failures are logged, never raised — revocation must not
    break logout.
    """
    jti = payload.get("jti")
    if not jti:
        return
    try:
        from api.cache import get_client

        client = get_client()
        if client is None:
            logger.warning("Redis unavailable — access-token jti %r not blocklisted", jti)
            return
        exp = payload.get("exp")
        ttl = max(int(exp - datetime.now(timezone.utc).timestamp()), 1) if exp else 3600
        # L51: single atomic SET with the remaining-lifetime TTL.
        await client.set(_BLOCKLIST_KEY_PREFIX + jti, 1, ex=ttl)
    except Exception:
        logger.exception("Failed to blocklist access-token jti %r", jti)


async def _is_token_revoked(jti: str | None) -> bool:
    """True when the token's ``jti`` is on the revocation blocklist."""
    if not jti:
        return False
    try:
        from api.cache import get_client

        client = get_client()
        if client is None:
            return False
        return bool(await client.exists(_BLOCKLIST_KEY_PREFIX + jti))
    except Exception:
        logger.exception("Blocklist lookup failed for jti %r — failing open", jti)
        return False


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an access token.

    Returns the decoded payload on success.
    Raises ``jwt.ExpiredSignatureError`` or ``jwt.InvalidTokenError`` on failure.
    """
    payload = pyjwt.decode(
        token,
        get_config().JWT_SECRET,
        algorithms=[JWT_ALGORITHM],  # pinned to ["HS256"] — see H4
        options={"require": ["sub", "exp"]},
    )
    # Validate token type is explicitly "access" to prevent refresh token reuse
    if payload.get("type") != "access":
        raise pyjwt.InvalidTokenError("Token type must be 'access'")
    # Validate required claims are present and correct
    if not isinstance(payload.get("sub"), int):
        raise pyjwt.InvalidTokenError("Invalid subject claim")
    if payload.get("role") not in ("admin", "user"):
        raise pyjwt.InvalidTokenError("Invalid role claim")
    return payload


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


async def _authenticate_user() -> tuple[Any | None, Any | None]:
    """Authenticate from the ``Authorization`` header, returning ``(user, error)``.

    On success returns ``(User, None)`` where ``User`` is the active DB row;
    on failure returns ``(None, problem_response)`` where ``problem_response``
    is a ready-to-return RFC 7807 ``(body, status, headers)`` tuple. Shared by
    ``jwt_required`` and ``admin_required`` so both decorators are
    order-independent (M-11).

    Supported scheme (M15): only ``Bearer`` (RFC 6750) is supported — a
    request MUST send ``Authorization: Bearer <jwt>``. ``Token``/``JWT``
    prefixes are not accepted and yield a 401. Do not add other schemes here
    without documenting them; if a proxy ever prepends a different scheme
    (M15), it must strip it before this handler runs. The scheme match is
    case-insensitive (RFC 7235 / L-30).
    """
    auth = request.headers.get("Authorization", "")
    # RFC 7235 auth-scheme names are case-insensitive, so accept "bearer " too
    # (L-30).
    if not auth.lower().startswith("bearer "):
        return None, problem_json(
            401, "Unauthorized", "Missing or malformed Authorization header"
        )

    token = auth[7:].strip()
    try:
        payload = decode_access_token(token)
    except pyjwt.ExpiredSignatureError:
        logger.warning("Rejected expired access token")
        return None, problem_json(401, "Token expired", "Access token has expired")
    except pyjwt.InvalidTokenError:
        logger.warning("Rejected invalid access token")
        return None, problem_json(401, "Invalid token", "Access token is not valid")

    # H7: reject tokens revoked via the jti blocklist (logout / password change).
    if await _is_token_revoked(payload.get("jti")):
        logger.warning("Rejected revoked access token (jti=%s)", payload.get("jti"))
        return None, problem_json(401, "Invalid token", "Access token is not valid")

    user_id: int = payload.get("sub")

    # M12: hit the short-TTL Redis cache for the common case, falling back to
    # the DB on a miss or Redis outage.
    user = await _get_user_cached(user_id)
    if user is None or not user.is_active:
        # Return the same generic detail as an invalid token so a caller
        # cannot learn whether the account was deactivated (N-11; L-26 class).
        return None, problem_json(
            401, "Unauthorized", "Access token is not valid"
        )
    return user, None


def jwt_required(f: Callable) -> Callable:
    """Require a valid JWT access token.

    Attaches the ``User`` instance to ``g.current_user``.
    """

    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> Any:
        user, error = await _authenticate_user()
        if error is not None:
            return error
        g.current_user = user
        return await f(*args, **kwargs)

    return decorated


def admin_required(f: Callable) -> Callable:
    """Require a valid JWT with ``role == 'admin'``.

    Order-independent with ``@jwt_required`` (M-11): if stacked below it,
    ``g.current_user`` is already populated and we just check the role; if
    stacked above it (``@admin_required`` over ``@jwt_required``), this
    decorator authenticates the request itself before the role check, so it no
    longer depends on ``@jwt_required`` running first. An unauthenticated
    request gets 401, a non-admin user gets 403.
    """

    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> Any:
        user = getattr(g, "current_user", None)
        if user is None:
            user, error = await _authenticate_user()
            if error is not None:
                return error
            g.current_user = user
        if user.role != "admin":
            return problem_json(403, "Forbidden", "Admin privileges are required")
        return await f(*args, **kwargs)

    return decorated
