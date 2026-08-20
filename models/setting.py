"""
SystemSetting model — configurable system knobs stored in the DB.

Admins can tweak behaviour (thresholds, feature flags, etc.) without
touching code.  Changes persist across restarts.
"""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(
        String(100), primary_key=True,
    )
    value: Mapped[str] = mapped_column(
        Text, nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Relationships ─────────────────────────────────────────────────────
    updated_by_user = relationship(
        "User", back_populates="settings_updated",
        foreign_keys=[updated_by],
    )

    def __repr__(self) -> str:
        return f"<SystemSetting key='{self.key}' value='{self.value}'>"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g., "system_settings"
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g., setting key
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # e.g., "update", "delete"
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # ── Relationships ─────────────────────────────────────────────────────
    changed_by_user = relationship(
        "User", back_populates="audit_entries",
        foreign_keys=[changed_by],
    )
