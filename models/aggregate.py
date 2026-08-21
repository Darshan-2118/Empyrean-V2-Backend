"""
HourlyAgg model — pre-computed hourly summaries per node.

This is a regular PostgreSQL table, filled by the Celery aggregation task
(``tasks/aggregation.py``) each hour.  It is *not* a materialized view and is
*not* yet a TimescaleDB continuous aggregate.  Whether to migrate it to a
continuous aggregate (the extension is already installed by migration
``0002_add_timescaledb_hypertable``) is a future decision and is not currently planned.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, PrimaryKeyConstraint, REAL, SmallInteger, String
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class HourlyAgg(Base):
    __tablename__ = "hourly_agg"

    bucket: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
    )
    node_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )

    avg_temperature: Mapped[float | None] = mapped_column(REAL, nullable=True)
    avg_humidity: Mapped[float | None] = mapped_column(REAL, nullable=True)
    avg_pm25: Mapped[float | None] = mapped_column(REAL, nullable=True)
    avg_pm10: Mapped[float | None] = mapped_column(REAL, nullable=True)
    max_aqi: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    min_aqi: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    avg_aqi: Mapped[float | None] = mapped_column(REAL, nullable=True)
    anomaly_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0",
    )
    reading_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0",
    )

    # ── Constraints ───────────────────────────────────────────────────────
    __table_args__ = (
        PrimaryKeyConstraint("bucket", "node_id", name="hourly_agg_pkey"),
    )

    # ── Relationships ─────────────────────────────────────────────────────
    node = relationship("Node", back_populates="hourly_aggregates")

    def __repr__(self) -> str:
        return (
            f"<HourlyAgg bucket={self.bucket} node_id='{self.node_id}' "
            f"avg_aqi={self.avg_aqi}>"
        )
