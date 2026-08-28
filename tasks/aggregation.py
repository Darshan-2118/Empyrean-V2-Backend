"""Aggregation and data-retention Celery tasks.

Hourly roll-ups of raw ``sensor_readings`` into per-node ``hourly_agg``
summaries, plus periodic purging of readings older than the configured
retention window.

Both tasks run synchronously on a single ``get_sync_db()`` session (Celery
workers have no async event loop) and operate on a raw SQL UPSERT / DELETE so
the hypertable's composite ``(time, node_id)`` constraints are respected.

Issue #28: Added daily cleanup of expired refresh tokens to prevent database bloat.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, func

from celery_app import _TASK_AUTORETRY, celery_app
from config import get_config
from models import SystemSetting, RefreshToken
from models.base import get_sync_db

logger = logging.getLogger("empyrean.tasks.aggregation")

cfg = get_config()

# Keep last N refresh tokens per user even if expired (Issue #28)
_REFRESH_TOKEN_RETENTION_PER_USER = 10
# M48: …but only for this long after expiry. The keep-last-N carve-out used
# to preserve N expired tokens per user *forever*; tokens expired beyond the
# grace window are deleted regardless of the per-user retention.
_REFRESH_TOKEN_GRACE_DAYS = 30


# ``name`` defaults to the fully-qualified path (tasks.aggregation.hourly_aggregate)
# which is exactly what the beat schedule references.
@celery_app.task(name="empyrean.tasks.aggregation.hourly_aggregate", **_TASK_AUTORETRY)
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

        # Watermark: the most recent hour already rolled up.  L71: start 24h
        # behind it (inclusive) — H25 accepts device timestamps up to 24h in
        # the past, so late readings can land in already-closed hours; the
        # idempotent UPSERT folds them back in on the next run.
        watermark = session.execute(
            text("SELECT MAX(bucket) FROM hourly_agg")
        ).scalar()
        start = earliest if watermark is None else max(earliest, watermark - timedelta(hours=24))

        if start >= end:
            logger.info("Aggregation up to date — watermark %s >= end %s", watermark, end)
            return {"buckets": 0}

        result = session.execute(sql, {"start": start, "end": end})
        n = result.rowcount

    logger.info("Hourly aggregation for [%s, %s) upserted %s rows", start, end, n)
    return {"buckets": n}


def _retention_days(session) -> int:
    """Resolve the retention window from the ``data_retention_days`` setting.

    Mirrors ``tasks.alerts._threshold``: the ``system_settings`` row wins;
    otherwise (or when its value is not a positive number) fall back to
    ``DATA_RETENTION_DAYS`` config. This is what makes the Phase 10 admin
    ``PATCH /admin/settings`` knob take effect on the cleanup task.
    """
    raw = session.scalar(
        select(SystemSetting.value).where(SystemSetting.key == "data_retention_days")
    )
    if raw is not None:
        try:
            return int(float(raw))
        except ValueError:
            logger.warning(
                "Setting data_retention_days=%r is not numeric — using config fallback",
                raw,
            )
    return int(cfg.DATA_RETENTION_DAYS)


@celery_app.task(name="empyrean.tasks.aggregation.data_retention_cleanup", **_TASK_AUTORETRY)
def data_retention_cleanup() -> dict:
    """Delete readings older than the configured retention period.

    Retention comes from the ``data_retention_days`` system setting (default
    ``DATA_RETENTION_DAYS`` = 365); expired rows are purged from
    ``sensor_readings`` in one pass. The ``days`` value is interpolated into
    the ``interval`` literal from an int resolved by :func:`_retention_days` —
    never user-supplied raw SQL.

    Returns ``{"deleted": n}`` where ``n`` is the number of rows removed.
    """
    # L-8: refuse a non-positive *config* fallback before touching the DB. The
    # setting may still override it to a valid window below, but a mis-set
    # config must never lead to a purge query or even a session open.
    if cfg.DATA_RETENTION_DAYS <= 0:
        logger.warning(
            "DATA_RETENTION_DAYS=%s — refusing to purge readings (must be > 0)",
            cfg.DATA_RETENTION_DAYS,
        )
        return {"deleted": 0}

    with get_sync_db() as session:
        days = _retention_days(session)  # setting wins, config fallback
        if days <= 0:
            logger.warning(
                "effective retention %s — refusing to purge readings (must be > 0)",
                days,
            )
            return {"deleted": 0}

        sql = text(
            "DELETE FROM sensor_readings WHERE time < now() - (:days * interval '1 day')"
        )
        result = session.execute(sql, {"days": days})
        n = result.rowcount

        # L48: hourly_agg roll-ups must obey the same retention window — the
        # purge used to touch only sensor_readings, so buckets accumulated
        # forever beyond retention.
        agg_result = session.execute(
            text("DELETE FROM hourly_agg WHERE bucket < now() - (:days * interval '1 day')"),
            {"days": days},
        )
        agg_deleted = agg_result.rowcount

        # H15: long-deactivated accounts must not keep PII forever. Once an
        # inactive user has been gone past the retention window, scrub the
        # identity fields in place — FK history (alerts/settings/audit rows)
        # survives via its SET NULL / plain references, but nothing links back
        # to a person any more.
        anon_sql = text(
            """
            UPDATE users SET
                username = 'deleted-user-' || id::text,
                email = 'deleted-user-' || id::text || '@deleted.invalid',
                password_hash = '!',
                notification_prefs = '{}'::jsonb,
                last_login_at = NULL
            WHERE is_active = false
              AND updated_at < now() - (:days * interval '1 day')
              AND email NOT LIKE '%@deleted.invalid'
            """
        )
        anon_result = session.execute(anon_sql, {"days": days})
        anonymized = anon_result.rowcount

    logger.info(
        "Data-retention cleanup removed %s readings, %s hourly buckets, "
        "and anonymised %s deactivated user(s)",
        n, agg_deleted, anonymized,
    )
    return {"deleted": n, "hourly_buckets_deleted": agg_deleted, "users_anonymised": anonymized}


@celery_app.task(name="empyrean.tasks.aggregation.refresh_token_cleanup", **_TASK_AUTORETRY)
def refresh_token_cleanup() -> dict:
    """Delete expired refresh tokens to prevent database bloat (Issue #28).

    Runs daily (beat entry added by M79). Deletes tokens where
    expires_at < now(). Keeps the last _REFRESH_TOKEN_RETENTION_PER_USER
    tokens per user even if expired, to support legitimate use cases like
    revoking all sessions — but only for _REFRESH_TOKEN_GRACE_DAYS after
    expiry (M48); the carve-out used to keep them forever.

    Returns ``{"deleted": n}`` where ``n`` is the number of rows removed.
    """
    with get_sync_db() as session:
        # Subquery to find expired tokens to delete, keeping the latest N per
        # user. Uses a window function to rank tokens by created_at desc per
        # user. M48: tokens expired beyond the grace window are deleted even
        # when inside the keep-last-N carve-out.
        sql = text("""
            DELETE FROM refresh_tokens
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           expires_at,
                           ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC) as rn
                    FROM refresh_tokens
                    WHERE expires_at < now()
                ) ranked
                WHERE rn > :retention
                   OR expires_at < now() - (:grace_days * interval '1 day')
            )
        """)
        result = session.execute(
            sql,
            {
                "retention": _REFRESH_TOKEN_RETENTION_PER_USER,
                "grace_days": _REFRESH_TOKEN_GRACE_DAYS,
            },
        )
        n = result.rowcount

    logger.info(
        "Refresh token cleanup removed %s expired tokens (keeping last %s per "
        "user for up to %s days after expiry)",
        n, _REFRESH_TOKEN_RETENTION_PER_USER, _REFRESH_TOKEN_GRACE_DAYS,
    )
    return {"deleted": n}