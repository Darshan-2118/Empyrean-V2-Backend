"""Aggregation and data-retention Celery tasks.

Hourly roll-ups of raw ``sensor_readings`` into per-node ``hourly_agg``
summaries, plus periodic purging of readings older than the configured
retention window.

Both tasks run synchronously on a single ``get_sync_db()`` session (Celery
workers have no async event loop) and operate on a raw SQL UPSERT / DELETE so
the hypertable's composite ``(time, node_id)`` constraints are respected.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from celery_app import celery_app
from config import get_config
from models.base import get_sync_db

logger = logging.getLogger("empyrean.tasks.aggregation")

cfg = get_config()


# ``name`` defaults to the fully-qualified path (tasks.aggregation.hourly_aggregate)
# which is exactly what the beat schedule references.
@celery_app.task
def hourly_aggregate() -> dict:
    """Roll up every complete-but-unaggregated hour of readings into ``hourly_agg``.

    The target buckets are all UTC hours strictly before the current hour
    boundary (M-7 backfill).  ``end`` is that boundary; ``start`` is derived
    from the earliest reading hour and a watermark on ``hourly_agg``, so a run
    after downtime covers *all* missed hours (a single grouped UPSERT over the
    ``[start, end)`` window) instead of only the most recent one.  For each node
    we compute averages (temperature, humidity, pm25, pm10, aqi) plus min/max
    aqi, an anomaly count and a reading count, via ``INSERT ... ON CONFLICT
    (bucket, node_id) DO UPDATE`` so re-runs are idempotent.

    Returns ``{"buckets": n}`` where ``n`` is the number of node/bucket rows
    written (or updated).
    """
    # Exclusive upper bound of the last fully-elapsed hour, in UTC. Windows are
    # half-open ``[start, end)`` so an in-progress hour is never aggregated.
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # The bucket is re-derived inside SQL from the row timestamps.
    sql = text(
        """
        INSERT INTO hourly_agg (
            bucket, node_id,
            avg_temperature, avg_humidity, avg_pm25, avg_pm10,
            max_aqi, min_aqi, avg_aqi,
            anomaly_count, reading_count
        )
        SELECT
            -- Truncate in UTC (M-3) so an hour never splits across buckets
            -- even if the DB session timezone is offset from UTC, and re-tag
            -- as timestamptz so the value matches the `hourly_agg.bucket` col.
            date_trunc('hour', time AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS bucket,
            node_id,
            AVG(temperature)                                AS avg_temperature,
            AVG(humidity)                                   AS avg_humidity,
            AVG(pm25)                                       AS avg_pm25,
            AVG(pm10)                                       AS avg_pm10,
            MAX(aqi)                                        AS max_aqi,
            MIN(aqi)                                        AS min_aqi,
            AVG(aqi)                                        AS avg_aqi,
            COUNT(*) FILTER (WHERE is_anomaly)              AS anomaly_count,
            COUNT(*)                                        AS reading_count
        FROM sensor_readings
        WHERE time >= :start AND time < :end
        GROUP BY bucket, node_id
        ON CONFLICT (bucket, node_id) DO UPDATE SET
            avg_temperature = EXCLUDED.avg_temperature,
            avg_humidity    = EXCLUDED.avg_humidity,
            avg_pm25        = EXCLUDED.avg_pm25,
            avg_pm10        = EXCLUDED.avg_pm10,
            max_aqi         = EXCLUDED.max_aqi,
            min_aqi         = EXCLUDED.min_aqi,
            avg_aqi         = EXCLUDED.avg_aqi,
            anomaly_count   = EXCLUDED.anomaly_count,
            reading_count   = EXCLUDED.reading_count
        """
    )

    with get_sync_db() as session:
        # Earliest hour that holds any data older than the last complete hour.
        # If there is no data at all there is nothing to aggregate.
        earliest = session.execute(
            text(
                "SELECT MIN(date_trunc('hour', time AT TIME ZONE 'UTC') "
                "AT TIME ZONE 'UTC') FROM sensor_readings WHERE time < :end"
            ),
            {"end": end},
        ).scalar()
        if earliest is None:
            logger.info("No readings to aggregate before %s", end)
            return {"buckets": 0}

        # Watermark: the most recent hour already rolled up.  Start from it
        # (inclusive) so late-arriving readings for the last aggregated hour are
        # folded back in by the idempotent UPSERT on the next run.
        watermark = session.execute(
            text("SELECT MAX(bucket) FROM hourly_agg")
        ).scalar()
        start = earliest if watermark is None else max(earliest, watermark)

        if start >= end:
            logger.info("Aggregation up to date — watermark %s >= end %s", watermark, end)
            return {"buckets": 0}

        result = session.execute(sql, {"start": start, "end": end})
        n = result.rowcount

    logger.info("Hourly aggregation for [%s, %s) upserted %s rows", start, end, n)
    return {"buckets": n}


@celery_app.task
def data_retention_cleanup() -> dict:
    """Delete readings older than the configured retention period.

    Retention comes from ``DATA_RETENTION_DAYS`` (default 365); expired rows
    are purged from ``sensor_readings`` in one pass.

    Returns ``{"deleted": n}`` where ``n`` is the number of rows removed.
    """
    days = cfg.DATA_RETENTION_DAYS
    if days <= 0:
        logger.warning(
            "DATA_RETENTION_DAYS=%s — refusing to purge readings (must be > 0)", days
        )
        return {"deleted": 0}
    # ``days`` is a trusted int from config — safe to interpolate (the delete
    # window is intentionally not user-supplied).
    sql = text(
        f"DELETE FROM sensor_readings WHERE time < now() - interval '{days} days'"
    )

    with get_sync_db() as session:
        result = session.execute(sql)
        n = result.rowcount

    logger.info("Data-retention cleanup removed %s readings", n)
    return {"deleted": n}