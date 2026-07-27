"""
Alert model — threshold-breach notifications.

When AQI (or another parameter) crosses a threshold, the system logs an
alert here.  The frontend displays unacknowledged alerts on a map.
"""

from sqlalchemy import ForeignKey, Integer, REAL, String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class Alert(Base):
    __tablename__ = "alerts"

    alert_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    node_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("nodes.node_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    parameter: Mapped[str] = mapped_column(
        String(50), nullable=False,
    )
    value: Mapped[float] = mapped_column(
        REAL, nullable=False,
    )
    threshold: Mapped[float] = mapped_column(
        REAL, nullable=False,
    )
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False,
    )
    message: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    triggered_at = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(),
    )
    acknowledged_at = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    acknowledged_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Relationships ─────────────────────────────────────────────────────
    node = relationship("Node", back_populates="alerts")
    acknowledger = relationship(
        "User", back_populates="acknowledged_alerts",
        foreign_keys=[acknowledged_by],
    )

    def __repr__(self) -> str:
        return (
            f"<Alert alert_id={self.alert_id} node_id='{self.node_id}' "
            f"severity='{self.severity}' acknowledged={self.acknowledged_at is not None}>"
        )
