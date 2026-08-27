"""
Alert model — threshold-breach notifications.

When AQI (or another parameter) crosses a threshold, the system logs an
alert here.  The frontend displays unacknowledged alerts on a map.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, REAL, String, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


# Maximum message length for alert messages to prevent DoS via storage exhaustion (#24)
_MAX_ALERT_MESSAGE_LENGTH = 10000


class Alert(Base):
    __tablename__ = "alerts"

    # Partial unique index backing the escalation-aware alert de-dupe (M-4).
    # At most one *unacknowledged* alert may exist per (node_id, parameter), so
    # the DB — not a racy application-side check-then-insert — arbitrates
    # double-inserts. ``tasks.check_thresholds`` upserts against this index.
    # Delivered by migration ``0003_add_alerts_partial_unique.py``;
    # this declaration keeps alembic autogenerate + ``create_all`` in sync.
    #
    # L25: the message cap used to be enforced only in the task; the CHECK
    # constraint bounds storage at the DB level too (NULL messages pass).
    # M64: partial ``(triggered_at DESC)`` index backs the hot unacked-listing
    # query (``WHERE acknowledged_at IS NULL ORDER BY triggered_at DESC``).
    # Both delivered by migration ``0006_add_alert_constraints.py``.
    __table_args__ = (
        Index(
            "uq_alerts_unacked_node",
            "node_id",
            "parameter",
            unique=True,
            postgresql_where=text("acknowledged_at IS NULL"),
        ),
        Index(
            "idx_alerts_unacked_triggered",
            text("triggered_at DESC"),
            postgresql_where=text("acknowledged_at IS NULL"),
        ),
        CheckConstraint(
            f"message IS NULL OR LENGTH(message) <= {_MAX_ALERT_MESSAGE_LENGTH}",
            name="ck_alerts_message_length",
        ),
    )

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
    triggered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(),
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
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
