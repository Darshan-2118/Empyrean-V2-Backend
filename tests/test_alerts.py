"""
Alert-task tests — escalation-aware de-dupe (M-4) and recency bounding (M-5).

These require the ``empyrean_test`` PostgreSQL DB that ``tests/conftest.py``
creates (``create_all`` also builds the partial unique index declared on the
``Alert`` model, which the ``ON CONFLICT`` upsert in ``tasks/alerts.py``
targets). Unit-level tests exercise ``_latest_aqi`` / ``_upsert_alert`` on the
in-fixture transaction; end-to-end tests seed committed rows and call the
``check_thresholds`` Celery task directly (no broker needed).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select

from models import Alert, Node, SensorReading, SystemSetting
from models.base import get_sync_db
from tasks.alerts import _AQI_RECENCY, _latest_aqi, _upsert_alert, check_thresholds

_NOW = datetime.now(timezone.utc)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _seed_node(session, node_id: str) -> Node:
    node = session.get(Node, node_id)
    if node is None:
        node = Node(node_id=node_id, reading_interval=30, is_active=True)
        session.add(node)
        session.flush()
    return node


def _seed_reading(session, node_id: str, *, aqi: int, time=None) -> None:
    _seed_node(session, node_id)
    session.add(
        SensorReading(time=time or _NOW, node_id=node_id, aqi=aqi, is_anomaly=False)
    )
    session.flush()


def _seed_settings(session, *, alerts_enabled: str = "true") -> None:
    session.add_all(
        [
            SystemSetting(key="alerts_enabled", value=alerts_enabled),
            SystemSetting(key="aqi_warning_threshold", value="100"),
            SystemSetting(key="aqi_critical_threshold", value="150"),
        ]
    )
    session.flush()


def _alerts_for(session, node_id: str) -> list[Alert]:
    return list(session.scalars(select(Alert).where(Alert.node_id == node_id)).all())


def _alert_count(session, node_id: str) -> int:
    return session.scalar(
        select(func.count()).select_from(Alert).where(Alert.node_id == node_id)
    ) or 0


# ── M-5: _latest_aqi recency bound ───────────────────────────────────────────


def test_latest_aqi_ignores_stale_reading(db_session):
    _seed_reading(
        db_session, "STALE-01", aqi=200,
        time=_NOW - _AQI_RECENCY - timedelta(minutes=5),
    )
    assert _latest_aqi(db_session, "STALE-01") is None


def test_latest_aqi_returns_fresh_reading(db_session):
    _seed_reading(db_session, "FRESH-01", aqi=120, time=_NOW)
    assert _latest_aqi(db_session, "FRESH-01") == 120


def test_latest_aqi_picks_most_recent_fresh(db_session):
    nid = "MIXED-01"
    _seed_reading(db_session, nid, aqi=110, time=_NOW - timedelta(minutes=8))
    _seed_reading(db_session, nid, aqi=160, time=_NOW)
    assert _latest_aqi(db_session, nid) == 160


# ── M-4: _upsert_alert escalation-aware upsert ───────────────────────────────


def test_upsert_suppresses_equal_or_lower_severity(db_session):
    nid = "SUPPRESS-01"
    _seed_node(db_session, nid)
    # Equal severity: second warning is suppressed, still one row.
    assert _upsert_alert(db_session, nid, 120, 100, "warning", "w1") is True
    assert _upsert_alert(db_session, nid, 125, 100, "warning", "w2") is False
    rows = _alerts_for(db_session, nid)
    assert len(rows) == 1
    assert rows[0].severity == "warning"
    assert rows[0].message == "w1"


def test_upsert_escalates_warning_to_critical(db_session):
    """M-4 root cause: an unacknowledged warning must NOT block a critical."""
    nid = "ESCALATE-01"
    _seed_node(db_session, nid)
    assert _upsert_alert(db_session, nid, 120, 100, "warning", "w") is True
    # Critical breach outranks the outstanding warning → upgraded in place.
    assert _upsert_alert(db_session, nid, 180, 150, "critical", "c") is True
    rows = _alerts_for(db_session, nid)
    assert len(rows) == 1  # still one unacknowledged alert
    assert rows[0].severity == "critical"
    assert rows[0].value == 180
    assert rows[0].message == "c"
    # A later warning is suppressed by the critical.
    assert _upsert_alert(db_session, nid, 130, 100, "warning", "w2") is False
    assert _alerts_for(db_session, nid)[0].severity == "critical"


def test_upsert_unique_index_allows_distinct_nodes():
    """A node with an unacknowledged alert does not suppress another node's."""
    nid_a, nid_b = "NODE-A", "NODE-B"
    with get_sync_db() as session:
        try:
            _seed_node(session, nid_a)
            _seed_node(session, nid_b)
            assert _upsert_alert(session, nid_a, 120, 100, "warning", "a") is True
            assert _upsert_alert(session, nid_b, 120, 100, "warning", "b") is True
            count = session.scalar(
                select(func.count())
                .select_from(Alert)
                .where((Alert.node_id == nid_a) | (Alert.node_id == nid_b))
            )
            assert count == 2
        finally:
            session.execute(delete(Node).where(Node.node_id.in_([nid_a, nid_b])))
            session.commit()


# ── end-to-end: check_thresholds ─────────────────────────────────────────────


def _run_with_seed(setup, verify=None) -> dict:
    """Seed committed rows, run the task, run *verify*, then clean up."""
    result = None
    with get_sync_db() as session:
        node_ids = setup(session)
        session.commit()
    try:
        result = check_thresholds()
        if verify is not None:
            verify()
    finally:
        with get_sync_db() as session:
            session.execute(delete(Node).where(Node.node_id.in_(node_ids)))
            session.execute(
                delete(SystemSetting).where(
                    SystemSetting.key.in_(
                        ["alerts_enabled", "aqi_warning_threshold", "aqi_critical_threshold"]
                    )
                )
            )
            session.commit()
    return result


def test_check_thresholds_creates_alert_for_fresh_breach():
    def verify():
        with get_sync_db() as session:
            assert _alert_count(session, "RTC-01") == 1

    def seed(session):
        _seed_settings(session)
        _seed_reading(session, "RTC-01", aqi=160, time=_NOW)
        return ["RTC-01"]

    assert _run_with_seed(seed, verify=verify) == {"created": 1}


def test_check_thresholds_skips_stale_reading():
    """M-5: an offline node's old high AQI must not fire/refire alerts."""

    def seed(session):
        _seed_settings(session)
        _seed_reading(
            session, "RTC-STALE", aqi=200,
            time=_NOW - _AQI_RECENCY - timedelta(minutes=5),
        )
        return ["RTC-STALE"]

    assert _run_with_seed(seed) == {"created": 0}


def test_check_thresholds_respects_alerts_enabled_toggle():
    def seed(session):
        _seed_settings(session, alerts_enabled="false")
        _seed_reading(session, "RTC-OFF", aqi=160, time=_NOW)
        return ["RTC-OFF"]

    assert _run_with_seed(seed) == {"created": 0}


# ── L-7: alerts_enabled missing-row fallback ─────────────────────────────────


def test_check_thresholds_fires_when_alerts_enabled_setting_missing():
    """L-7: a missing ``alerts_enabled`` row must NOT disable alerts.

    On a fresh DB there are no ``system_settings`` rows, so the master toggle
    must fall back to enabled — a monitoring system should err on the side of
    alerting rather than silently missing a critical breach.
    """

    def verify():
        with get_sync_db() as session:
            assert _alert_count(session, "RTC-NOSET") == 1

    def seed(session):
        # Deliberately NO _seed_settings — no system_settings rows at all.
        _seed_reading(session, "RTC-NOSET", aqi=160, time=_NOW)
        return ["RTC-NOSET"]

    assert _run_with_seed(seed, verify=verify) == {"created": 1}


# ── L-8: data_retention_cleanup non-positive guard ───────────────────────────


@pytest.mark.parametrize("days", [0, -7])
def test_data_retention_cleanup_refuses_nonpositive_retention(days, monkeypatch):
    """L-8: a mis-set non-positive retention window must not purge readings.

    The guard short-circuits before any SQL is built or a session is opened;
    ``get_sync_db`` is patched to raise, so reaching it means the guard failed.
    """
    from tasks import aggregation as agg_task

    def _fail_if_opened(*_args, **_kwargs):
        raise AssertionError(
            "data_retention_cleanup must not open a DB session for days<=0"
        )

    monkeypatch.setattr(agg_task.cfg, "DATA_RETENTION_DAYS", days)
    monkeypatch.setattr(agg_task, "get_sync_db", _fail_if_opened)

    assert agg_task.data_retention_cleanup() == {"deleted": 0}