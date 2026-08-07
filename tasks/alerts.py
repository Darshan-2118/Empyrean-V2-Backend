"""Alert-related Celery tasks.

``check_thresholds`` evaluates each active node's latest reading against the
configured AQI thresholds and, when breached, records an ``Alert`` row. The
alert is *escalation-aware* (M-4): an unacknowledged alert for a node only
suppresses a new one of **lower or equal** severity — an unacknowledged
warning never blocks a later critical. Rows are upserted against a partial
unique index ``(node_id, parameter) WHERE acknowledged_at IS NULL`` so the
DB (not a racy check-then-insert) arbitrates double-inserts, and only
*fresh* readings (last 10 minutes, M-5) can fire an alert.

All work runs synchronously on one ``get_sync_db()`` session. Thresholds and
the master ``alerts_enabled`` toggle come from ``system_settings`` with
fallback to config.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from celery_app import celery_app
from config import get_config
from models import Alert, Node, SensorReading, SystemSetting
from models.base import get_sync_db
from mqtt.publisher import publish_alert
from tasks._redis import BEAT_HEARTBEAT_KEY, get_sync_redis

logger = logging.getLogger("empyrean.tasks.alerts")

cfg = get_config()

# Settings keys and the config fields we fall back to when a key is absent.
_AQI_WARNING_SETTING = "aqi_warning_threshold"
_AQI_CRITICAL_SETTING = "aqi_critical_threshold"
_ALERTS_ENABLED_SETTING = "alerts_enabled"

# M-5: a reading older than this cannot fire/refire an alert, so an offline
# node's last high-AQI reading stops alerting after the window passes.
_AQI_RECENCY = timedelta(minutes=10)

# Severity ordering used by the atomic ON CONFLICT upsert (M-4). The DO UPDATE
# only fires when the *new* severity strictly outranks the existing one.
_SEVERITY_RANK_SQL = (
    "CASE {table}.severity WHEN 'critical' THEN 2 "
    "WHEN 'warning' THEN 1 ELSE 0 END"
)


def _stamp_beat_heartbeat() -> None:
    """Record that the beat scheduler fired a task (liveness for /admin/health).

    ``check_thresholds`` is the most frequent beat task (every 60s), so it is
    the natural heartbeat proving beat is actually dispatching scheduled work.
    Stamped through the shared sync Redis client under ``BEAT_HEARTBEAT_KEY``
    with a 1h TTL; ``GET /admin/health`` reports ``celery_beat`` healthy while
    the stamp is fresh (≤ 3× the schedule interval). Fail-soft: no client or a
    write error ⇒ warn and continue — a dead Redis must not fail the task.
    """
    client = get_sync_redis()
    if client is None:
        return
    try:
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        client.set(BEAT_HEARTBEAT_KEY, stamp, ex=3600)
    except Exception:
        logger.warning("Failed to stamp beat heartbeat in Redis")


def _read_settings(session) -> dict:
    """Load all ``system_settings`` into a ``{key: value}`` dict (best-effort)."""
    rows = session.scalars(select(SystemSetting)).all()
    return {s.key: s.value for s in rows}


def _threshold(settings: dict, key: str, fallback: int) -> float:
    """Resolve a numeric threshold from settings, falling back to config."""
    raw = settings.get(key)
    try:
        return float(raw) if raw is not None else float(fallback)
    except ValueError:
        logger.warning("Setting %s=%r is not numeric — using config fallback", key, raw)
        return float(fallback)


def _latest_aqi(session, node_id: str) -> float | None:
    """Return the AQI of the node's most recent *fresh* reading, or ``None``.

    Bounds recency to ``_AQI_RECENCY`` (M-5): a reading older than the window
    is treated as "no data", so an offline node with a lingering high AQI stops
    re-firing alerts on every beat instead of alerting forever.
    """
    cutoff = datetime.now(timezone.utc) - _AQI_RECENCY
    stmt = (
        select(SensorReading.aqi)
        .where(
            SensorReading.node_id == node_id,
            SensorReading.aqi.is_not(None),
            SensorReading.time >= cutoff,
        )
        .order_by(SensorReading.time.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def _upsert_alert(
    session, node_id: str, aqi: float, threshold: float, severity: str, message: str
) -> bool:
    """Atomically record or upgrade an unacknowledged alert (M-4).

    Uses PostgreSQL ``INSERT … ON CONFLICT`` targeted at the partial unique
    index ``(node_id, parameter) WHERE acknowledged_at IS NULL``, so two racing
    workers can never double-insert and no check-then-insert window exists.

    Escalation semantics live in the DO UPDATE ``WHERE``: the existing
    unacknowledged row is replaced only when the new severity *outranks* it
    (warning → critical). A new alert of equal/lower severity conflicts and is
    suppressed (the ``WHERE`` is false → no-op). Returns ``True`` when a row
    was inserted or upgraded, ``False`` when suppressed.
    """
    existing_rank = _SEVERITY_RANK_SQL.format(table="alerts")
    new_rank = _SEVERITY_RANK_SQL.format(table="EXCLUDED")
    stmt = (
        pg_insert(Alert)
        .values(
            node_id=node_id,
            parameter="aqi",
            value=aqi,
            threshold=threshold,
            severity=severity,
            message=message,
        )
        .on_conflict_do_update(
            index_elements=[Alert.node_id, Alert.parameter],
            index_where=Alert.acknowledged_at.is_(None),
            set_={
                "value": aqi,
                "threshold": threshold,
                "severity": severity,
                "message": message,
            },
            where=text(f"{existing_rank} < {new_rank}"),
        )
    )
    return bool(session.execute(stmt).rowcount)


@celery_app.task
def check_thresholds() -> dict:
    """Create threshold-breach alerts for active nodes' latest readings.

    Runs every minute (see beat schedule). Skips entirely unless the master
    ``alerts_enabled`` setting is ``"true"``. For each active node with a
    *fresh* (last 10 min) ``aqi``, compares against the warning (100) and
    critical (150) thresholds. A breach is suppressed only when an existing
    unacknowledged alert for that node+``aqi`` parameter is of **equal or
    higher** severity; a higher-severity breach upgrades the existing row.
    Rows are upserted atomically against the partial unique index
    ``(node_id, parameter) WHERE acknowledged_at IS NULL``.

    Returns ``{"created": n}``.
    """
    _stamp_beat_heartbeat()  # liveness for /admin/health (Phase 10)

    with get_sync_db() as session:
        settings = _read_settings(session)

        # Master toggle — anything other than exactly "true" disables alerts.
        # A *missing* row (fresh DB) defaults to "true" so alerts stay ON.
        if settings.get(_ALERTS_ENABLED_SETTING, "true") != "true":
            logger.info("Alerts disabled (alerts_enabled != 'true') — skipping")
            return {"created": 0}

        warning = _threshold(
            settings, _AQI_WARNING_SETTING, cfg.AQI_WARNING_THRESHOLD
        )
        critical = _threshold(
            settings, _AQI_CRITICAL_SETTING, cfg.AQI_CRITICAL_THRESHOLD
        )

        # One grouped query instead of an N+1 per active node: latest *fresh*
        # AQI per active node (same recency bound as _latest_aqi, M-5).
        cutoff = datetime.now(timezone.utc) - _AQI_RECENCY
        latest_rows = session.execute(
            select(SensorReading.node_id, SensorReading.aqi, SensorReading.aqi_category)
            .join(Node, Node.node_id == SensorReading.node_id)
            .where(
                Node.is_active.is_(True),
                SensorReading.aqi.is_not(None),
                SensorReading.time >= cutoff,
            )
            .distinct(SensorReading.node_id)
            .order_by(SensorReading.node_id, SensorReading.time.desc())
        ).all()

        created = 0
        publishes: list[dict[str, object]] = []  # deferred until after commit
        for node_id, aqi, category in latest_rows:
            if aqi is None:
                continue  # no reading / no aqi for this node yet

            if aqi >= critical:
                severity, threshold = "critical", critical
            elif aqi >= warning:
                severity, threshold = "warning", warning
            else:
                continue  # within limits — nothing to alert

            # M-4: atomic upsert against the partial unique index. An existing
            # unacknowledged alert of >= severity suppresses this one; a higher-
            # severity breach upgrades the existing row. ``created`` reflects
            # rows actually inserted or upgraded (suppressions count as 0).
            if _upsert_alert(
                session,
                node_id,
                aqi,
                threshold,
                severity,
                message=(
                    f"AQI {aqi:.0f} exceeded the {severity} threshold "
                    f"{threshold:.0f} on node {node_id}"
                ),
            ):
                created += 1
                logger.info(
                    "Created/upgraded %s alert for node %s (aqi=%.0f >= %.0f)",
                    severity, node_id, aqi, threshold,
                )
                # Defer the broadcast: publish only once the transaction has
                # committed (get_sync_db commits on with-block exit) so a WS
                # client that receives the push will also find the alert row in
                # GET /alerts. publish_alert never raises, so a broker outage
                # cannot fail the beat task or roll back the committed rows.
                publishes.append(
                    {
                        "node_id": node_id,
                        "aqi": float(aqi),
                        "category": category,
                        "severity": severity,
                        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                )

    for publish in publishes:
        publish_alert(**publish)

    logger.info("check_thresholds created %s alert(s)", created)
    return {"created": created}