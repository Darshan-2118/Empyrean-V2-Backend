"""
PasswordResetToken model — one-time, expiring server-side password-reset tokens.

Tokens are issued by ``POST /auth/forgot-password`` and redeemed (one-time) by
``POST /auth/reset-password``. The raw token is never stored: only a SHA-256
digest (mirroring :class:`RefreshToken`), so a DB leak cannot be replayed to
reset an account. ``expires_at`` bounds validity and ``used_at`` enforces the
one-time contract; the cleanup task sweeps expired/used rows.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Always a 64-char SHA-256 hex digest (see api.auth.make_reset_token) —
    # the raw token is returned to the client once and never persisted.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, index=True,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────
    user = relationship("User")

    def __repr__(self) -> str:
        return (
            f"<PasswordResetToken id={self.id} user_id={self.user_id} "
            f"used={self.used_at is not None}>"
        )
