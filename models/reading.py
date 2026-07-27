"""
SensorReading model — the core time-series data from sensor nodes.

Each row represents a single sensor reading enriched with computed values
(fuzzy_score, aqi, aqi_category, is_anomaly).

This is designed as a regular table now; it will be converted to a
TimescaleDB hypertable once the extension is installed.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, PrimaryKeyConstraint, REAL, SmallInteger, String, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class SensorReading(Base):
    __tablename__ = "sensor_readings"

    # Use (time, node_id) as composite PK — works with both regular tables
    # and TimescaleDB hypertables (where the PK must include the partition column).
    time = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
    )
    node_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )

    # Raw sensor data
    temperature: Mapped[float | None] = mapped_column(REAL, nullable=True)
    humidity: Mapped[float | None] = mapped_column(REAL, nullable=True)
    pressure: Mapped[float | None] = mapped_column(REAL, nullable=True)
    voc_ohm: Mapped[float | None] = mapped_column(REAL, nullable=True)
    mq135_ppm: Mapped[float | None] = mapped_column(REAL, nullable=True)
    pm1: Mapped[float | None] = mapped_column(REAL, nullable=True)
    pm25: Mapped[float | None] = mapped_column(REAL, nullable=True)
    pm10: Mapped[float | None] = mapped_column(REAL, nullable=True)
    battery_v: Mapped[float | None] = mapped_column(REAL, nullable=True)

    # Enriched data (added by Celery worker after fuzzy inference)
    fuzzy_score: Mapped[float | None] = mapped_column(REAL, nullable=True)
    aqi: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    aqi_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_anomaly: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text('false'), nullable=False,
    )

    # ── Constraints ───────────────────────────────────────────────────────
    __table_args__ = (
        PrimaryKeyConstraint("time", "node_id", name="sensor_readings_pkey"),
    )

    # ── Relationships ─────────────────────────────────────────────────────
    node = relationship("Node", back_populates="sensor_readings")

    def __repr__(self) -> str:
        return (
            f"<SensorReading time={self.time} node_id='{self.node_id}' "
            f"aqi={self.aqi}>"
        )
