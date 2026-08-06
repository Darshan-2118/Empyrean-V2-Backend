"""
Node model — each ESP32 sensor device out in the field gets a row here.

The ``node_id`` (e.g. ``"ESP32-01"``) comes from the device itself and
serves as the primary key, avoiding extra joins when processing readings.
"""

from datetime import datetime

from sqlalchemy import Boolean, Double, Integer, String, func, true
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class Node(Base):
    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(
        String(50), primary_key=True,
    )
    name: Mapped[str | None] = mapped_column(
        String(100), nullable=True,
    )
    location_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True,
    )
    lat: Mapped[float | None] = mapped_column(
        Double, nullable=True,
    )
    lon: Mapped[float | None] = mapped_column(
        Double, nullable=True,
    )
    firmware_version: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
    )
    reading_interval: Mapped[int] = mapped_column(
        Integer, default=30, nullable=False, server_default="30",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=true(),
    )
    registered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(),
    )
    last_seen: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )

    # ── Relationships ─────────────────────────────────────────────────────
    # passive_deletes=True: the DB already declares ON DELETE CASCADE on the
    # FKs, so let Postgres delete child rows instead of the ORM loading every
    # reading into memory (an OOM hazard on a hypertable).
    sensor_readings = relationship(
        "SensorReading", back_populates="node",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    hourly_aggregates = relationship(
        "HourlyAgg", back_populates="node",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    alerts = relationship(
        "Alert", back_populates="node",
        cascade="all, delete-orphan", passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Node node_id='{self.node_id}' name='{self.name}'>"
