"""
Readings blueprint — latest enriched reading per node + time-bucketed history.

Both endpoints require a valid JWT and are rate-limited per IP (200 req/min).

* ``GET /readings/latest``  — Redis-cached (``readings:latest``, TTL 60s).
* ``GET /readings/history`` — raw ``time_bucket`` aggregation over the
  ``sensor_readings`` hypertable, no cache.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from quart import Blueprint, jsonify, request
from sqlalchemy import select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import ProgrammingError

from api._time import parse_iso_datetime
from api.cache import cache_get_json, cache_set_json
from api.jwt import _problem_json, jwt_required
from api.rate_limit import rate_limit
from api.schemas import HistoryBucket, LatestReading
from models import Node, SensorReading
from models.base import AsyncSessionLocal, async_engine

logger = logging.getLogger("empyrean.readings")

readings_bp = Blueprint("readings", __name__)

# Cache keys (see api/cache.py docstring and docs/database.md).
_LATEST_CACHE_KEY = "readings:latest"
_LATEST_CACHE_TTL = 60

# ``bucket`` query-param value → TimescaleDB ``time_bucket`` interval.
_BUCKET_INTERVALS = {
    "1m": "1 minute",
    "5m": "5 minutes",
    "15m": "15 minutes",
    "1h": "1 hour",
    "6h": "6 hours",
    "1d": "1 day",
}

# Maximum ``from``→``to`` span allowed per bucket granularity (M-15). A 1-minute
# bucket over a multi-year range would otherwise emit hundreds of thousands of
# buckets in one response — a DB/CPU/memory spike. The bound scales up for
# coarser buckets so coarse queries can still span a long period.
_BUCKET_MAX_SPAN = {
    "1m": timedelta(days=30),
    "5m": timedelta(days=120),
    "15m": timedelta(days=365),
    "1h": timedelta(days=730),
    "6h": timedelta(days=1095),
    "1d": timedelta(days=3650),
}


def _latest_from_reading(r: SensorReading) -> dict:
    """Serialize a ``SensorReading`` ORM row via the ``LatestReading`` DTO."""
    return LatestReading(
        node_id=r.node_id,
        time=r.time,
        temperature=r.temperature,
        humidity=r.humidity,
        pressure=r.pressure,
        pm25=r.pm25,
        pm10=r.pm10,
        battery_v=r.battery_v,
        fuzzy_score=r.fuzzy_score,
        aqi=r.aqi,
        aqi_category=r.aqi_category,
        is_anomaly=r.is_anomaly,
    ).model_dump()


def _to_float(value) -> float | None:
    """Coerce a Postgres numeric (REAL/Decimal) to ``float``, preserving ``None``."""
    return float(value) if value is not None else None


def _history_from_row(row: RowMapping) -> dict:
    """Serialize one ``time_bucket`` result row via the ``HistoryBucket`` DTO."""
    return HistoryBucket(
        bucket=row["bucket"],
        node_id=row["node_id"],
        avg_temperature=_to_float(row["avg_temperature"]),
        avg_humidity=_to_float(row["avg_humidity"]),
        avg_pm25=_to_float(row["avg_pm25"]),
        avg_pm10=_to_float(row["avg_pm10"]),
        avg_aqi=_to_float(row["avg_aqi"]),
        max_aqi=row["max_aqi"],
        min_aqi=row["min_aqi"],
        reading_count=row["reading_count"],
    ).model_dump()


@readings_bp.route("/latest", methods=["GET"])
@rate_limit()
@jwt_required
async def latest():
    """Latest enriched reading per **active** node.

    Single ``DISTINCT ON (node_id)`` query, not one query per node. Result is
    cached under the **global** key ``readings:latest`` (TTL 60s) — the only
    key this endpoint serves. Per-node keys are intentionally *not* written
    here (L-28): nothing on the API side reads ``readings:latest:{node_id}``,
    and writing them was dead work on every cache miss. Response shape:
    ``{"readings": [...]}``.
    """
    cached = await cache_get_json(_LATEST_CACHE_KEY)
    if cached is not None:
        return jsonify({"readings": cached}), 200

    stmt = (
        select(SensorReading)
        .join(Node, Node.node_id == SensorReading.node_id)
        .where(Node.is_active.is_(True))
        .distinct(SensorReading.node_id)
        .order_by(SensorReading.node_id, SensorReading.time.desc())
    )
    async with AsyncSessionLocal() as session:
        result = await session.execute(stmt)
        readings = list(result.scalars().all())

    payload = [_latest_from_reading(r) for r in readings]

    await cache_set_json(_LATEST_CACHE_KEY, payload, _LATEST_CACHE_TTL)

    return jsonify({"readings": payload}), 200


@readings_bp.route("/history", methods=["GET"])
@rate_limit()
@jwt_required
async def history():
    """Time-bucketed historical readings via Postgres ``time_bucket``.

    Query params (all optional): ``from`` (ISO, default 24h ago), ``to`` (ISO,
    default now), ``node_id`` (str, default all nodes), ``bucket`` (one of
    ``1m/5m/15m/1h/6h/1d``, default ``1h``). The requested range is clamped to
    the granularity's ``_BUCKET_MAX_SPAN`` (M-15) so a 1-minute bucket can't
    produce millions of buckets. Response shape: ``{"buckets": [...]}``.
    """
    bucket = request.args.get("bucket", "1h")
    if bucket not in _BUCKET_INTERVALS:
        return _problem_json(
            422,
            "Unprocessable Entity",
            f"bucket must be one of: {', '.join(_BUCKET_INTERVALS)}",
        )

    now = datetime.now(timezone.utc)
    try:
        from_dt = parse_iso_datetime(
            request.args.get("from"), default=now - timedelta(hours=24)
        )
        to_dt = parse_iso_datetime(request.args.get("to"), default=now)
    except ValueError as exc:
        return _problem_json(422, "Unprocessable Entity", str(exc))

    if from_dt >= to_dt:
        return _problem_json(
            422, "Unprocessable Entity", "'from' must be earlier than 'to'"
        )

    # M-15: clamp the span to what the bucket granularity can serve without
    # exploding into millions of buckets. Keep the *most recent* segment of the
    # requested range (rolling ``from`` forward) so the response still reflects
    # the latest history the client asked about.
    max_span = _BUCKET_MAX_SPAN[bucket]
    if to_dt - from_dt > max_span:
        from_dt = to_dt - max_span
        logger.warning(
            "history range exceeds %s for bucket=%s — clamped from to %s",
            max_span, bucket, from_dt.isoformat(),
        )

    node_id = request.args.get("node_id") or None

    where = "time >= :from_ts AND time <= :to_ts"
    params: dict = {"from_ts": from_dt, "to_ts": to_dt}
    if node_id:
        where += " AND node_id = :node_id"
        params["node_id"] = node_id

    # ``interval`` comes from the fixed _BUCKET_INTERVALS mapping, never user
    # input, so it is safe to interpolate (time_bucket needs a constant here).
    sql = text(f"""
        SELECT
            time_bucket('{_BUCKET_INTERVALS[bucket]}', time) AS bucket,
            node_id,
            AVG(temperature) AS avg_temperature,
            AVG(humidity) AS avg_humidity,
            AVG(pm25) AS avg_pm25,
            AVG(pm10) AS avg_pm10,
            AVG(aqi) AS avg_aqi,
            MAX(aqi) AS max_aqi,
            MIN(aqi) AS min_aqi,
            COUNT(*) AS reading_count
        FROM sensor_readings
        WHERE {where}
        GROUP BY bucket, node_id
        ORDER BY bucket, node_id
    """)

    async with async_engine.connect() as conn:
        try:
            result = await conn.execute(sql, params)
            rows = result.mappings().all()
        except ProgrammingError as exc:
            # The history query relies on the TimescaleDB ``time_bucket``
            # function. If the extension is not installed the query raises
            # ``function time_bucket does not exist``. That is a 503 "misconfigured
            # backend", not a 500 the client can do nothing about (#09).
            if "time_bucket" in str(exc).lower():
                logger.warning(
                    "/readings/history failed: TimescaleDB extension missing — %s",
                    exc,
                )
                return _problem_json(
                    503,
                    "Service Unavailable",
                    "History requires the TimescaleDB extension (time_bucket). "
                    "Install it with `CREATE EXTENSION timescaledb` and convert "
                    "sensor_readings to a hypertable.",
                )
            raise

    buckets = [_history_from_row(row) for row in rows]
    return jsonify({"buckets": buckets}), 200
