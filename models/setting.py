"""
SystemSetting model — configurable system knobs stored in the DB.

Admins can tweak behaviour (thresholds, feature flags, etc.) without
touching code.  Changes persist across restarts.
"""

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
    updated_at = mapped_column(
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
