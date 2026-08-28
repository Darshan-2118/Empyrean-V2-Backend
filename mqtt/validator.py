"""
Pydantic v2 payload validation for inbound MQTT messages.

``ReadingPayload`` and ``StatusPayload`` mirror the device contract from
``docs/mqtt.md``. Validation helpers return ``None`` on failure (logging a
warning) so the MQTT client thread never crashes on a malformed message.

The body ``node_id`` is **not authoritative** (H-3): the topic id parsed by
``mqtt/client.py`` overrides it before validation, so this field is optional
and only used as a cross-check. A compliant topic-only device is not dropped.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mqtt.config import _NODE_ID_RE

logger = logging.getLogger("empyrean.mqtt")

# Same pattern as mqtt/config.py — no slashes, no MQTT wildcards. Applied to the
# optional body node_id so a spoofed id can never be used to build topics.
_NODE_ID_PATTERN = _NODE_ID_RE.pattern

# Float sensor fields that must never silently accept ``bool`` (L-17) and are
# covered by ``allow_inf_nan=False`` for +Inf/NaN rejection (L-16).
_FLOAT_FIELDS = (
    "temperature",
    "humidity",
    "pressure",
    "voc_ohm",
    "mq135_ppm",
    "pm1",
    "pm25",
    "pm10",
    "battery_v",
)


class ReadingPayload(BaseModel):
    """Full sensor reading published by a device to ``air/node/+/reading``."""

    # Reject +Infinity/NaN on every float field (L-16): Pydantic v2 defaults
    # to allow_inf_nan=True, and ``inf`` would pass the ``ge=0`` bounds.
    model_config = ConfigDict(allow_inf_nan=False)

    node_id: str | None = Field(None, pattern=_NODE_ID_PATTERN)
    time: datetime | None = None
    temperature: float | None = Field(None, ge=-40, le=60)
    humidity: float | None = Field(None, ge=0, le=100)
    # Loosened from 900–1100 to the Bosch BME680 physical range so high-altitude
    # sites (~795 hPa at ~2000 m) are accepted (L-19).
    pressure: float | None = Field(None, ge=300, le=1250)
    voc_ohm: float | None = Field(None, ge=0)
    mq135_ppm: float | None = Field(None, ge=0)
    pm1: float | None = Field(None, ge=0, le=2000)
    pm25: float | None = Field(None, ge=0, le=2000)
    pm10: float | None = Field(None, ge=0, le=2000)
    battery_v: float | None = Field(None, ge=0, le=5)

    @field_validator(*_FLOAT_FIELDS, mode="before")
    @classmethod
    def _reject_bool_as_float(cls, v):
        """Reject ``bool`` before numeric coercion (L-17).

        ``bool`` is a ``int`` subclass, so a misconfigured device sending
        ``"temperature": true`` would otherwise silently become a plausible
        ``1.0`` °C reading.
        """
        if v is None:
            return v
        if isinstance(v, bool):
            raise ValueError("bool is not a valid numeric sensor value")
        return v


class StatusPayload(BaseModel):
    """Heartbeat published by a device to ``air/node/+/status``."""

    online: bool
    battery_v: float | None = Field(None, ge=0, le=5)
    # L70: bound the firmware string so a device can't bloat validated
    # payloads with an unbounded value (pairs with the _MAX_PAYLOAD_BYTES
    # drop in mqtt/client.py).
    firmware: str | None = Field(None, max_length=64)


def validate_reading(data: dict) -> "ReadingPayload | None":
    """Validate an inbound reading payload, returning ``None`` on failure.

    Invalid payloads are logged and dropped rather than raised, keeping the
    MQTT client thread alive on bad device data.
    """
    try:
        return ReadingPayload.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError — swallowed on purpose
        logger.warning("Dropping invalid reading payload: %s", exc)
        return None


def validate_status(data: dict) -> "StatusPayload | None":
    """Validate an inbound status/heartbeat payload, ``None`` on failure."""
    try:
        return StatusPayload.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError — swallowed on purpose
        logger.warning("Dropping invalid status payload: %s", exc)
        return None
