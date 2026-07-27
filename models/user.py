"""
User model — both admins and regular users live here.

The ``role`` column (``'admin'`` | ``'user'``) controls access level.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    username: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True,
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True,
    )
    password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user",
    )
    notification_prefs: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
    )
    last_login_at = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    created_at = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(),
    )
    updated_at = mapped_column(
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

    def __repr__(self) -> str:
        return f"<User id={self.id} username='{self.username}' role='{self.role}'>"
