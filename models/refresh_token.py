"""
RefreshToken model — server-side storage for JWT refresh tokens.

Enables logout (set ``revoked = True``) and refresh-token rotation
(revoke the old token when issuing a new one).
"""

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True,
    )
    expires_at = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
    )
    created_at = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(),
    )
    revoked: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
    )

    # ── Relationships ─────────────────────────────────────────────────────
    user = relationship("User", back_populates="refresh_tokens")

    def __repr__(self) -> str:
        return (
            f"<RefreshToken id={self.id} user_id={self.user_id} "
            f"revoked={self.revoked}>"
        )
