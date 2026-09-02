"""
Auth blueprint — registration (auto-login), login, token refresh, and logout.

Every endpoint returns RFC 7807 ``application/problem+json`` on error.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from quart import Blueprint, jsonify, request
from sqlalchemy import func, select, update as sa_update
from sqlalchemy.exc import IntegrityError

from api.jwt import (
    problem_json,
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from api.rate_limit import rate_limit
from api.schemas import (
    AuthResponse,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    UserBrief,
)
from api.validation import validate_body, validated_body
from config import get_config
from models.base import AsyncSessionLocal
from models.helpers import hash_password
from models.password_reset_token import PasswordResetToken
from models.refresh_token import RefreshToken
from models.user import User

logger = logging.getLogger("empyrean.auth")

auth_bp = Blueprint("auth", __name__)


def _bootstrap_admin_credentials() -> tuple[str, str, str] | None:
    """Return ``(username, password, email)`` from env config, or ``None``.

    H5/H6: credentials are never hardcoded in source. The operator opts in by
    setting ``BOOTSTRAP_ADMIN_USERNAME`` + ``BOOTSTRAP_ADMIN_PASSWORD`` in the
    environment; when either is missing there is no provisioned admin path at
    all (use ``scripts/seed.py`` with ``SEED_ADMIN_PASSWORD`` instead).
    """
    cfg = get_config()
    username = cfg.BOOTSTRAP_ADMIN_USERNAME.strip()
    password = cfg.BOOTSTRAP_ADMIN_PASSWORD
    if not username or not password:
        return None
    email = cfg.BOOTSTRAP_ADMIN_EMAIL.strip() or f"{username.lower()}@empyrean.local"
    return username, password, email


async def ensure_hardcoded_admin() -> User | None:
    """Ensure the env-configured bootstrap admin exists with role='admin'.

    Returns ``None`` (and logs once) when no bootstrap credentials are
    configured — that is a valid production posture.
    """
    creds = _bootstrap_admin_credentials()
    if creds is None:
        logger.info(
            "No BOOTSTRAP_ADMIN_USERNAME/PASSWORD configured — skipping admin "
            "auto-provisioning (seed via scripts/seed.py if needed)"
        )
        return None
    username, password, email = creds

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.username == username)
        )
        user = result.scalar_one_or_none()
        if user is None:
            # M91: only an EXACT username match may be promoted. If a
            # case-variant row exists it belongs to whoever registered it —
            # never touch it, never apply the bootstrap password to an
            # existing row; refuse and let the operator resolve the clash.
            variant = await session.execute(
                select(User).where(func.lower(User.username) == username.lower())
            )
            if variant.scalar_one_or_none() is not None:
                logger.error(
                    "Bootstrap admin '%s' NOT provisioned: a case-variant "
                    "account already exists and will not be promoted",
                    username,
                )
                return None
            pwd_hash = await asyncio.to_thread(hash_password, password)
            user = User(
                username=username,
                email=email,
                password_hash=pwd_hash,
                role="admin",
                is_active=True,
                notification_prefs={"email_on_critical": True},
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info("Created bootstrap admin user '%s'", username)
        else:
            changed = False
            if user.role != "admin":
                user.role = "admin"
                changed = True
            if not user.is_active:
                user.is_active = True
                changed = True
            if changed:
                await session.commit()
                await session.refresh(user)
                # M12: role/is_active changed on a cached row — evict it.
                from api.jwt import invalidate_user_cache

                await invalidate_user_cache(user.id)
        return user

# bcrypt hash of a dummy password, compared against when a login username does
# not exist — so unknown usernames take the same time as a wrong password and
# the endpoint does not leak which usernames are registered. Computed lazily on
# first use (L-33): None at module import, set on the first unknown-username
# login. All subsequent unknown-username logins reuse the cached hash, so
# timing stays consistent (500ms) after the first warm-up.
_DUMMY_PASSWORD_HASH: str | None = None


def _dummy_password_hash() -> str:
    """Return the lazy dummy bcrypt hash, computing it on first call (L-33)."""
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


def _refresh_expiry(now: datetime, cfg: Any) -> datetime:
    """Refresh-token expiry time for ``now``, truncated to the second.

    L44: truncation drops only the sub-second fraction — the old
    ``second=0`` form truncated to the *minute*, silently shortening token
    validity by up to 59 s relative to the documented contract.
    """
    return now.replace(microsecond=0) + timedelta(
        days=cfg.JWT_REFRESH_TOKEN_EXPIRY_DAYS
    )


# M9: internal-only counter for refresh-token reuse (theft) events. Kept out
# of the client response so an attacker never learns they were detected, but
# incrementable via Redis so ops can alert when credentials are compromised.
_TOKEN_REUSE_KEY = "auth:refresh_token_reuse_total"
_TOKEN_REUSE_TTL_S = 86400  # daily window, refreshed on every hit


async def _record_token_reuse() -> None:
    """Increment the internal refresh-token-reuse counter (best-effort)."""
    try:
        from api.cache import get_client

        client = get_client()
        if client is None:
            return
        await client.incr(_TOKEN_REUSE_KEY)
        await client.expire(_TOKEN_REUSE_KEY, _TOKEN_REUSE_TTL_S)
    except Exception:
        logger.exception("Failed to record refresh-token reuse counter")


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


async def _issue_auth_tokens(user: User, session=None) -> tuple:
    """Create a JWT pair, persist the refresh token, return a 201 response.

    M8: ``session`` is the caller's already-open ``AsyncSession``. Reusing it
    (instead of opening a second one) collapses the token issuance into the
    caller's single transaction, saving a round-trip and a second connection
    per login/register. When ``session`` is ``None`` a fresh one is opened for
    callers that don't have one handy.
    """
    if session is None:
        async with AsyncSessionLocal() as session:
            return await _issue_auth_tokens(user, session)

    access = create_access_token(user.id, user.role)
    raw_refresh, token_hash = generate_refresh_token()
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


@auth_bp.route("/register", methods=["POST"])
@rate_limit(5, 60)  # M-12: stricter per-IP cap — account creation is a spam vector
@validate_body(RegisterRequest)
async def register():
    """Register a new user and auto-login (return JWT tokens)."""
    data = validated_body()

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
            return problem_json(
                409,
                "Conflict",
                "Username or email already taken",
            )

        # M8: reuse the caller's open session so token issuance is one
        # transaction instead of opening a second session/round-trip.
        return await _issue_auth_tokens(user, session)


@auth_bp.route("/login", methods=["POST"])
@rate_limit(10, 60)  # M-12: brute-force throttle (10/min per IP)
@validate_body(LoginRequest)
async def login():
    """Authenticate with username/password, return JWT tokens."""
    data = validated_body()

    # H5/H6: the old hardcoded-admin login branch (plaintext credential compare
    # bypassing bcrypt, active in production) is removed. The bootstrap admin —
    # when configured via BOOTSTRAP_ADMIN_* env vars — is provisioned at startup
    # with a bcrypt hash and authenticates through the normal path below.

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
            return problem_json(401, "Unauthorized", "Invalid username or password")

        if not await asyncio.to_thread(
            bcrypt.checkpw, pwd_bytes, user.password_hash.encode("utf-8")
        ):
            logger.warning("Failed login: wrong password for %r", data.username)
            return problem_json(401, "Unauthorized", "Invalid username or password")

        if not user.is_active:
            logger.warning("Failed login: inactive user %r", data.username)
            return problem_json(401, "Unauthorized", "Invalid username or password")

        user.last_login_at = datetime.now(timezone.utc)
        await session.commit()

        # M8: reuse the open session for token issuance (single transaction).
        return await _issue_auth_tokens(user, session)


@auth_bp.route("/refresh", methods=["POST"])
@rate_limit(10, 60)  # M-12: per-IP cap on token rotation (brute-force surface)
@validate_body(RefreshRequest)
async def refresh():
    """Exchange a valid refresh token for a new JWT pair (token rotation)."""
    data = validated_body()

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

        if row is None:
            # H8: refresh-token reuse detection. A token that exists but is
            # already *revoked* must never be presented again — the only
            # legitimate holder stopped using it when it was rotated/logged out.
            # Re-presenting it is a theft signal, so revoke the entire user's
            # chain (the victim's next refresh fails and they re-authenticate).
            # An expired-but-never-revoked token stays a plain generic 401 with
            # no mutation (L-27) — idle expiry is benign, not theft.
            reused = await session.execute(
                select(RefreshToken.user_id).where(
                    RefreshToken.token_hash == token_hash,
                    RefreshToken.revoked == True,  # noqa: E712
                )
            )
            stolen_user_id = reused.scalar_one_or_none()
            if stolen_user_id is not None:
                logger.warning(
                    "Refresh-token reuse detected (user %s) — revoking all "
                    "of the user's refresh tokens as a theft response",
                    stolen_user_id,
                )
                await session.execute(
                    sa_update(RefreshToken)
                    .where(RefreshToken.user_id == stolen_user_id)
                    .values(revoked=True)
                )
                await session.commit()
                # M9: surface reuse as an internal-only counter so ops can alert
                # on credential theft without ever leaking it to the client
                # (the HTTP response stays a generic 401).
                await _record_token_reuse()
            logger.warning("Rejected refresh token: invalid, already revoked, or expired")
            return problem_json(
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
            return problem_json(
                401, "Unauthorized", "Refresh token is invalid or expired"
            )

        # Create new refresh token
        raw_new, new_hash = generate_refresh_token()
        new_expires = _refresh_expiry(now, get_config())

        # L43: no second UPDATE needed — the claiming UPDATE ... RETURNING at
        # the top of this handler already set revoked=True on this token.
        new_rt = RefreshToken(
            user_id=user.id,
            token_hash=new_hash,
            expires_at=new_expires,
        )
        session.add(new_rt)
        await session.commit()

    access = create_access_token(user.id, user.role)
    return jsonify(_auth_payload(user, access, raw_new)), 200


@auth_bp.route("/logout", methods=["POST"])
@rate_limit(10, 60)  # M-12: per-IP cap on token revocation (write-flood surface)
@validate_body(RefreshRequest)
async def logout():
    """Revoke a refresh token and the presented access token.

    L51: when a Bearer access token is present in the Authorization header it
    is added to the per-jti blocklist so it dies immediately instead of
    surviving for up to 15 minutes. Access-token revocation is best-effort
    and never breaks the always-204 contract.
    """
    data = validated_body()

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

    # L51: also revoke the access token presented in the Authorization header.
    from api.jwt import decode_access_token, revoke_access_token

    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        try:
            payload = decode_access_token(auth_header[7:].strip())
            await revoke_access_token(payload)
        except Exception:  # noqa: BLE001 — revocation is best-effort
            logger.warning("Could not revoke access token on logout")

    # Always return 204 — don't reveal whether the token existed
    return "", 204


# ── Password reset (forgot-password flow) ─────────────────────────────────────


def make_password_reset_token() -> tuple[str, str]:
    """Return ``(raw_token, sha256_hash)`` — the raw token is emailed once.

    Mirrors refresh-token storage: only the digest is persisted, so a DB leak
    cannot be replayed to reset an account (see ``models.password_reset_token``).
    """
    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


def hash_password_reset_token(raw_token: str) -> str:
    """Hash a raw reset token for DB lookup."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _send_password_reset_email(email: str, reset_url: str) -> None:
    """Best-effort outbound reset email (fail-soft, off the event loop).

    Runs the shared SMTP helper in a worker thread so the blocking handshake
    can never stall the HTTP request. Any failure is swallowed inside
    ``api.emailer.send_emails`` — the endpoint still answers 202, and the
    token simply expires unused.
    """
    try:
        asyncio.create_task(asyncio.to_thread(_email_reset, email, reset_url))
    except Exception:  # noqa: BLE001 — email must never break the request
        logger.warning("Could not schedule password-reset email send")


def _email_reset(email: str, reset_url: str) -> None:
    """Synchronous worker: send one password-reset email (fail-soft)."""
    from api.emailer import send_emails

    send_emails(
        [
            (
                "[Empyrean] Reset your password",
                (
                    "We received a request to reset your Empyrean password.\n\n"
                    f"Open the link below within the next {get_config().PASSWORD_RESET_TOKEN_EXPIRY_MINUTES} "
                    "minutes to choose a new one:\n\n"
                    f"{reset_url}\n\n"
                    "If you did not request this, you can safely ignore this email — "
                    "the link will expire on its own and your password is unchanged."
                ),
                email,
            )
        ]
    )


def _reset_link(raw_token: str) -> str:
    """Build the frontend reset URL embedding the (single-use) token."""
    base = get_config().PASSWORD_RESET_LINK_BASE.rstrip("/")
    return f"{base}?token={raw_token}"


@auth_bp.route("/forgot-password", methods=["POST"])
@rate_limit(5, 60)  # brute-force / enumeration throttle (5/min per IP)
@validate_body(ForgotPasswordRequest)
async def forgot_password():
    """Request a password-reset email for an account, without leaking its existence.

    Always returns ``202 Accepted`` with a generic message — whether or not the
    email has an account — so a caller can never learn which addresses are
    registered. If an account exists, a one-time reset token is minted (old
    unredeemed tokens for that user are invalidated) and the reset link is
    emailed. Email is best-effort and fail-soft.
    """
    data = validated_body()
    cfg = get_config()

    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.email == data.email))
        ).scalar_one_or_none()

        if user is not None and user.is_active:
            # Invalidate any previously-issued, unredeemed tokens so an older
            # reset link cannot be used after a new request supersedes it.
            await session.execute(
                sa_update(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.used_at.is_(None),
                )
                .values(used_at=datetime.now(timezone.utc))
            )
            raw, token_hash = make_password_reset_token()
            expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=cfg.PASSWORD_RESET_TOKEN_EXPIRY_MINUTES
            )
            session.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=token_hash,
                    expires_at=expires_at,
                )
            )
            await session.commit()

            _send_password_reset_email(user.email, _reset_link(raw))

    # Same response either way (no account enumeration).
    return (
        jsonify(
            {
                "message": (
                    "If an account exists for that email, a password reset link "
                    "has been sent."
                )
            }
        ),
        202,
    )


@auth_bp.route("/reset-password", methods=["POST"])
@rate_limit(10, 60)  # bruteforce throttle mirroring /login (10/min per IP)
@validate_body(ResetPasswordRequest)
async def reset_password():
    """Redeem a one-time reset token and set a new password.

    Requires the token emailed by ``forgot-password``. Enforces one-time use
    and expiry; on success the password is bcrypt-hashed, **all** of the user's
    refresh tokens are revoked (every session must re-authenticate), the user
    cache is invalidated, and the reset token is marked used. Any invalid,
    expired, or already-used token yields the same generic 401 so the endpoint
    cannot be used to probe token validity.
    """
    data = validated_body()
    token_hash = hash_password_reset_token(data.token)
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        token = (
            await session.execute(
                select(PasswordResetToken).where(
                    PasswordResetToken.token_hash == token_hash
                )
            )
        ).scalar_one_or_none()

        # Generic 401 for any invalid / expired / already-used token (never
        # distinguish the three — L-26 class).
        invalid = (
            token is None
            or token.used_at is not None
            or token.expires_at <= now
        )
        if invalid:
            return problem_json(
                401, "Unauthorized", "Reset token is invalid or has expired"
            )

        user = (
            await session.execute(
                select(User).where(User.id == token.user_id)
            )
        ).scalar_one_or_none()
        if user is None or not user.is_active:
            return problem_json(
                401, "Unauthorized", "Reset token is invalid or has expired"
            )

        user.password_hash = await asyncio.to_thread(hash_password, data.new_password)

        # Revoke every outstanding refresh token — a reset is a credential
        # change, so any live session must re-authenticate (mirrors
        # change-password / delete-account).
        result = await session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id,
                RefreshToken.revoked == False,  # noqa: E712
            )
        )
        for rt in result.scalars().all():
            rt.revoked = True

        # One-time contract: mark the token used (also supersedes within this
        # same request so a replayed token can never be redeemed twice).
        token.used_at = now
        await session.commit()

        # Evict the cached user row so the new password hash is effective
        # immediately (mirrors change-password's H37 invalidation).
        from api.jwt import invalidate_user_cache

        await invalidate_user_cache(user.id)

    logger.info("Password reset for user %s via reset token", user.username)
    return jsonify({"message": "Password reset successfully"}), 200
