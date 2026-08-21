"""Per-reading enrichment task — the heart of the Celery pipeline.

Takes one raw MQTT reading payload, enriches it (fuzzy score → EPA AQI →
Z-score anomaly), persists it as a ``SensorReading`` row, and writes the
result through to the ``readings:latest:{node_id}`` Redis key so the API's
latest-readings endpoint stays hot.

All steps run synchronously on a single ``get_sync_db()`` session (Celery
workers have no async event loop).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from celery_app import _TASK_AUTORETRY, celery_app
from fuzzy.tsukamoto import fuzzy_score
from models import Node, SensorReading
from models.base import get_sync_db
from tasks._redis import get_sync_redis
from tasks.aqi import compute_aqi

logger = logging.getLogger("empyrean.tasks.process_reading")

# Number of prior pm25 samples to load when computing the anomaly Z-score.
_ANOMALY_WINDOW_HOURS = 24
# 2880 = 24h at the standard 30s reporting cadence, so the loaded window matches
# the documented 24h (the old .limit(1000) capped it at ~8.3h — L-6).
_ANOMALY_WINDOW_SAMPLES = 2880
_ANOMALY_WINDOW_MINUTES = 1440  # 24 hours in minutes (time-based, not sample-count based)
_ANOMALY_MIN_SAMPLES = 5
_ANOMALY_Z_THRESHOLD = 3.0

# Cache key contract (docs/database.md).
_LATEST_CACHE_TTL = 60
# The API's ``GET /readings/latest`` serves this global key; it must be
# invalidated on every write-through so it never goes stale (L-28).
_LATEST_GLOBAL_KEY = "readings:latest"


def _to_float(value: float | str | None) -> float | None:
    """Coerce a numeric payload field to ``float``, preserving ``None``."""
    return float(value) if value is not None else None


def _valid_float(value) -> float | None:
    """Coerce a payload field to ``float``, mapping non-finite to ``None``.

    NaN/Inf sensor values are treated as *missing* (M-2) so they can never
    reach the fuzzy engine or AQI computation, which reject non-finite inputs.
    """
    value = _to_float(value)
    if value is not None and not math.isfinite(value):
        return None
    return value


def _build_reading(payload: dict, node_id: str) -> SensorReading:
    """Construct an enriched ``SensorReading`` from a raw payload dict."""
    temperature = _valid_float(payload.get("temperature"))
    humidity = _valid_float(payload.get("humidity"))
    pm25 = _valid_float(payload.get("pm25"))
    pm10 = _valid_float(payload.get("pm10"))

    # Fuzzy inputs must be numeric — treat missing temperature as 25.0 °C (the
    # domain midpoint, a neutral value) so a sub-zero / missing reading never
    # collapses to the 20.0 fallback (H-1); missing humidity/PM2.5 map to the
    # dry / clean-air shoulder. AQI is skipped separately when *both* PM
    # pollutants are absent.
    fuzzy_score_value = fuzzy_score(
        temperature if temperature is not None else 25.0,
        humidity if humidity is not None else 0.0,
        pm25 if pm25 is not None else 0.0,
    )

    aqi, aqi_category = compute_aqi(pm25, pm10)

    return SensorReading(
        time=_parse_time(payload.get("time")),
        node_id=node_id,
        temperature=temperature,
        humidity=humidity,
        pressure=_valid_float(payload.get("pressure")),
        voc_ohm=_valid_float(payload.get("voc_ohm")),
        mq135_ppm=_valid_float(payload.get("mq135_ppm")),
        pm1=_valid_float(payload.get("pm1")),
        pm25=pm25,
        pm10=pm10,
        battery_v=_valid_float(payload.get("battery_v")),
        fuzzy_score=fuzzy_score_value,
        aqi=aqi,
        aqi_category=aqi_category,
    )


def _parse_time(value: str | datetime | None) -> datetime:
    """Parse the payload ISO time, defaulting to now-UTC.

    Naive timestamps are treated as UTC (the MQTT ingest phase is expected to
    send UTC already). Accepts an already-parsed ``datetime`` (C-1: Kombu's JSON
    codec reconstructs ``datetime`` on the worker) and returns it as-is.
    
    Timestamps must be within ±24 hours of server time to prevent data injection
    attacks or device clock skew issues (#6).
    """
    if not value:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Unparseable reading time %r — defaulting to now-UTC", value)
            return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def detect_anomaly(session, node_id: str, pm25: float | None) -> bool:
    """Z-score anomaly flag for ``pm25`` against the node's last 24h of readings.

    Returns ``False`` when there is no pm25 value, fewer than 5 prior samples,
    or the prior values have zero variance (so no Z-score is meaningful).
    Logs the fallback reason so operators can detect data quality issues.
    """
    if pm25 is None:
        logger.warning(
            "detect_anomaly: skipped — pm25 is None for node %s",
            node_id,
            extra={"node_id": node_id, "pm25": pm25}
        )
        return False

    since = datetime.now(timezone.utc) - timedelta(minutes=_ANOMALY_WINDOW_MINUTES)
    # Aggregate mean/variance in SQL - use time-based window (not fixed sample count)
    inner = (
        select(SensorReading.pm25)
        .where(
            SensorReading.node_id == node_id,
            SensorReading.time >= since,
            SensorReading.pm25.is_not(None),
        )
        .order_by(SensorReading.time.desc())
        .limit(_ANOMALY_WINDOW_SAMPLES)
        .subquery()
    )
    count, mean, variance = session.execute(
        select(
            func.count(inner.c.pm25),
            func.avg(inner.c.pm25),
            func.var_pop(inner.c.pm25),
        )
    ).one()

    if not count or count < _ANOMALY_MIN_SAMPLES:
        logger.warning(
            "detect_anomaly: skipped — insufficient history for node %s (count=%s, min=%s)",
            node_id,
            count,
            _ANOMALY_MIN_SAMPLES,
            extra={"node_id": node_id, "history_count": count, "required_min": _ANOMALY_MIN_SAMPLES}
        )
        return False

    mean = float(mean)
    variance = float(variance)
    if variance == 0:
        logger.warning(
            "detect_anomaly: skipped — zero variance in history for node %s",
            node_id,
            extra={"node_id": node_id, "variance": variance}
        )
        return False

    z = abs(pm25 - mean) / (variance ** 0.5)
    is_anomaly = z > _ANOMALY_Z_THRESHOLD
    if is_anomaly:
        logger.info(
            "detect_anomaly: anomaly detected for node %s (pm25=%.2f, mean=%.2f, std=%.2f, z=%.2f > %.2f)",
            node_id, pm25, mean, variance ** 0.5, z, _ANOMALY_Z_THRESHOLD,
            extra={"node_id": node_id, "pm25": pm25, "mean": mean, "std": variance ** 0.5, "z": z, "threshold": _ANOMALY_Z_THRESHOLD}
        )
    return is_anomaly


def _enriched_dict(reading: SensorReading) -> dict:
    """Serialize a reading to the ``LatestReading`` cache shape (see api/schemas)."""
    return {
        "node_id": reading.node_id,
        "time": reading.time.isoformat().replace("+00:00", "Z"),
        "temperature": reading.temperature,
        "humidity": reading.humidity,
        "pressure": reading.pressure,
        "pm25": reading.pm25,
        "pm10": reading.pm10,
        "battery_v": reading.battery_v,
        "fuzzy_score": reading.fuzzy_score,
        "aqi": reading.aqi,
        "aqi_category": reading.aqi_category,
        "is_anomaly": reading.is_anomaly,
    }


def _get_redis_client():
    """Return the shared sync Redis client, creating it lazily.

    Delegates to :func:`tasks._redis.get_sync_redis` — one client per worker
    process, reused for all calls; ``None`` after a failed construction so
    callers degrade gracefully.
    """
    return get_sync_redis()


def _write_latest_cache(node_id: str, payload: dict) -> None:
    """Write-through to ``readings:latest:{node_id}``. Redis failure = log only."""
    try:
        client = _get_redis_client()
        client.setex(
            f"readings:latest:{node_id}", _LATEST_CACHE_TTL, json.dumps(payload)
        )
        # L-28: drop the served global key so the API's /readings/latest never
        # serves a snapshot older than this just-persisted reading.
        client.delete(_LATEST_GLOBAL_KEY)
    except Exception:
        logger.warning("Redis write-through failed for node %s — skipping", node_id)


def _is_duplicate_reading(exc: IntegrityError) -> bool:
    """True when ``exc`` is the composite-PK violation that marks a redelivery.

    Under ``task_acks_late=True`` (M-6) a reading can be redelivered after the
    original run already committed, so the insert collides with the existing
    ``(time, node_id)`` row.  We dedup *only* that constraint (``sensor_readings_pkey``);
    any other integrity error (FK/check) is re-raised so the task fails and the
    message is redelivered instead of being silently dropped (L-9).
    """
    orig = getattr(exc, "orig", None)
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
    return constraint == "sensor_readings_pkey"


@celery_app.task(name="empyrean.tasks.process_reading", **_TASK_AUTORETRY)
def process_reading(payload: dict) -> dict:
    """Enrich, persist, and cache one sensor reading. Empty dict = dropped."""
    node_id = payload.get("node_id")
    if not node_id:
        logger.warning("process_reading called without node_id — dropping")
        return {}

    with get_sync_db() as session:
        node = session.get(Node, node_id)
        if node is None or not node.is_active:
            logger.info("Dropping reading for unknown/inactive node %s", node_id)
            return {}

        reading = _build_reading(payload, node_id)
        # Anomaly is computed *before* insert so the pending row is not counted
        # against its own history.
        reading.is_anomaly = detect_anomaly(session, node_id, reading.pm25)

        try:
            session.add(reading)
            session.commit()
        except IntegrityError as exc:
            if not _is_duplicate_reading(exc):
                # A genuine FK/check violation — fail and let at-least-once
                # (acks_late + reject_on_worker_lost) redeliver it, rather than
                # masking a real DB error as a "duplicate" (L-9).
                session.rollback()
                raise
            logger.warning(
                "Duplicate reading for node %s at %s — dropped (redelivery)",
                node_id, reading.time,
            )
            session.rollback()
            return {}

        result = _enriched_dict(reading)

    _write_latest_cache(node_id, result)
    return result