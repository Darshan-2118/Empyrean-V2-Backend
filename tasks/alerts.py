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
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from celery_app import _TASK_AUTORETRY, celery_app
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

# Maximum length for alert messages (matches DB Text(10000) constraint)
_MAX_ALERT_MESSAGE_LENGTH = 10000

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

    L27: stamped at the END of the task, not at the start — a slow Redis (or a
    task that dies mid-run) must not advertise a heartbeat for work that has
    not actually completed.
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

    Issue #3: Severity comparison is now atomic with the upsert using raw SQL
    to ensure the severity ordering check cannot be bypassed by race conditions.

    Validates message length to prevent storage exhaustion (Issue #24).
    """
    # Validate message length to prevent storage exhaustion
    if message and len(message) > _MAX_ALERT_MESSAGE_LENGTH:
        logger.warning(
            "Alert message for node %s exceeds %d chars, truncating",
            node_id, _MAX_ALERT_MESSAGE_LENGTH
        )
        message = message[:_MAX_ALERT_MESSAGE_LENGTH]

    # Severity rank mapping: critical=2, warning=1, other=0
    severity_rank_map = {"critical": 2, "warning": 1}
    new_severity_rank = severity_rank_map.get(severity, 0)
    
    # Build the ON CONFLICT DO UPDATE with severity comparison in the WHERE clause
    # This ensures only higher-severity alerts replace existing ones.
    # H24: the new rank comes from the fixed Python map above and is int()-cast
    # before interpolation, so the SQL fragment can only ever embed a numeric
    # literal — even if a future caller passes user-controlled severity text.
    existing_rank = _SEVERITY_RANK_SQL.format(table="alerts")
    new_rank_value = int(severity_rank_map.get(severity, 0))
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
                "triggered_at": datetime.now(timezone.utc),
            },
            where=text(f"{existing_rank} < {new_rank_value}"),
        )
    )
    result = session.execute(stmt)
    return bool(result.rowcount)


# Settings key for the email-address that receives critical alerts, and the
# config field we fall back to when no row is set (mirrors the other knobs).
_ALERT_EMAIL_SETTING = "alert_email"


def _alert_email(session) -> str | None:
    """Resolve the critical-alert recipient address from ``system_settings`` (#11).

    The ``alert_email`` setting is the only recipient source (it is validated as
    an EmailStr by the admin API and stored as a row). Returns ``None`` when no
    address is configured, in which case email alerting is skipped entirely —
    fail-soft, never raises on a missing recipient. SMTP_* carry only transport
    credentials.
    """
    row = session.scalar(
        select(SystemSetting.value).where(SystemSetting.key == _ALERT_EMAIL_SETTING)
    )
    if row:
        return str(row)
    return None


def _send_alert_emails(
    recipient: str, alerts: list[tuple[str, float, str, str]]
) -> None:
    """Send all critical-breach alert emails over ONE SMTP connection (M52).

    ``alerts`` is a list of ``(node_id, aqi, severity, message)`` tuples. The
    old per-alert helper opened a fresh SMTP connection for each message —
    10 connects/min worst case with a noisy fleet; now one connection per beat
    run carries every message. Fail-soft (#11): any failure (unconfigured,
    auth error, network) is logged and swallowed so a beat task can never fail
    on email — the alert rows and MQTT/WS broadcasts are committed
    independently. A single message that fails mid-session is skipped without
    aborting the rest.
    """
    if not recipient or not alerts:
        return
    host, port, user, password, sender, use_tls = (
        cfg.SMTP_HOST,
        cfg.SMTP_PORT,
        cfg.SMTP_USERNAME,
        cfg.SMTP_PASSWORD,
        cfg.SMTP_FROM or recipient,
        cfg.SMTP_USE_TLS,
    )
    if not host:
        logger.debug(
            "SMTP_HOST not configured — skipping %d email alert(s)", len(alerts)
        )
        return
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if use_tls:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            sent = 0
            for node_id, aqi, severity, message in alerts:
                msg = EmailMessage()
                msg["Subject"] = f"[Empyrean] {severity.title()} AQI alert — node {node_id}"
                msg["From"] = sender
                msg["To"] = recipient
                msg.set_content(
                    f"AQI threshold breach on node {node_id}.\n\n"
                    f"Severity: {severity}\n"
                    f"AQI: {aqi:.0f}\n"
                    f"{message}\n\n"
                    f"Generated at {datetime.now(timezone.utc).isoformat()}"
                )
                try:
                    smtp.send_message(msg)
                    sent += 1
                except Exception:
                    logger.warning(
                        "Failed to send alert email for node %s to %s — skipped",
                        node_id, recipient,
                    )
        logger.info(
            "Sent %d/%d alert email(s) to %s", sent, len(alerts), recipient
        )
    except Exception:
        logger.warning(
            "Failed to send alert email batch (%d message(s)) to %s — skipped",
            len(alerts), recipient,
        )


@celery_app.task(name="empyrean.tasks.alerts.check_thresholds", **_TASK_AUTORETRY)
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
    with get_sync_db() as session:
        settings = _read_settings(session)

        # Master toggle — anything other than exactly "true" disables alerts.
        # A *missing* row (fresh DB) defaults to "true" so alerts stay ON.
        if settings.get(_ALERTS_ENABLED_SETTING, "true") != "true":
            logger.info("Alerts disabled (alerts_enabled != 'true') — skipping")
            _stamp_beat_heartbeat()  # L27: beat fired and the run completed
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
        emails: list[tuple[str, float, str, str]] = []  # (node, aqi, severity, msg)
        email_recipient: str | None = None
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
            message = (
                f"AQI {aqi:.0f} exceeded the {severity} threshold "
                f"{threshold:.0f} on node {node_id}"
            )
            if _upsert_alert(
                session,
                node_id,
                aqi,
                threshold,
                severity,
                message=message,
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
                # Email only critical breaches, and only when an address is set.
                if severity == "critical":
                    emails.append((node_id, float(aqi), severity, message))
                    # Get email recipient while session is still open
                    if email_recipient is None:
                        email_recipient = _alert_email(session)

    for publish in publishes:
        publish_alert(**publish)

    # Fail-soft email alerts for critical breaches (#11). Never raises; the
    # alert rows + WS/MQTT broadcasts above are already committed independently.
    # M52: one SMTP connection carries the whole batch instead of one per alert.
    if emails and email_recipient:
        _send_alert_emails(email_recipient, emails)

    _stamp_beat_heartbeat()  # L27: liveness for /admin/health, stamped on completion
    logger.info("check_thresholds created %s alert(s)", created)
    return {"created": created}