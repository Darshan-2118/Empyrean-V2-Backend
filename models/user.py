"""
User model — both admins and regular users live here.

The ``role`` column (``'admin'`` | ``'user'``) controls access level.
"""

from datetime import datetime

from sqlalchemy import Boolean, Integer, String, func, text, true
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    username: Mapped[str] = mapped_column(
        # H27: VARCHAR(n) in Postgres counts *characters*, not bytes, so the
        # audit's multibyte-overflow concern cannot trigger here — the API
        # schema caps usernames at 50 chars long before any write.
        String(50), unique=True, nullable=False, index=True,
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True,
    )
    password_hash: Mapped[str] = mapped_column(
        # H27: bcrypt digests are always 60 ASCII bytes; 255 chars leaves
        # generous headroom for future hash formats (argon2 ~97 chars).
        String(255), nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user",
    )
    notification_prefs: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=true(),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────
    refresh_tokens = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan",
    )
    acknowledged_alerts = relationship(
        "Alert", back_populates="acknowledger",
        foreign_keys="Alert.acknowledged_by",
    )
    settings_updated = relationship(
        "SystemSetting", back_populates="updated_by_user",
        foreign_keys="SystemSetting.updated_by",
    )
    audit_entries = relationship(
        "AuditLog", back_populates="changed_by_user",
        foreign_keys="AuditLog.changed_by",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username='{self.username}' role='{self.role}'>"
