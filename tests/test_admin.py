"""
Admin-phase tests — settings registry, PATCH validation, fail-soft health.

Focused unit/integration tests for Phase 10 (Admin endpoints), mirroring the
style of ``test_alerts.py``: the HTTP-level cumulative coverage lives in
``test_phase_coverage.py``; this file drills into the pure logic and the
fail-soft edges that are easy to miss at the route level.

Redis/DB are exercised through monkeypatched fakes where possible so the suite
stays fast; the ``data_retention_cleanup`` override test writes committed rows
to the ``empyrean_test`` DB like ``test_alerts``' end-to-end cases.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.schemas import AdminSettingsUpdate
from config import get_config
from models import Node, SensorReading, SystemSetting
from models.base import get_sync_db

cfg = get_config()

_SETTING_KEYS = (
    "aqi_warning_threshold",
    "aqi_critical_threshold",
    "data_retention_days",
    "alerts_enabled",
    "alert_email",
)


# ── Settings registry ─────────────────────────────────────────────────────────


def test_config_fallback_config_backed():
    """Config-backed knobs fall back to the live Config value."""
    from api.admin import _config_fallback

    assert _config_fallback("aqi_warning_threshold") == str(cfg.AQI_WARNING_THRESHOLD)
    assert _config_fallback("aqi_critical_threshold") == str(cfg.AQI_CRITICAL_THRESHOLD)
    assert _config_fallback("data_retention_days") == str(cfg.DATA_RETENTION_DAYS)


def test_config_fallback_fixed_defaults():
    """Non-config-backed knobs have a fixed, documented default."""
    from api.admin import _config_fallback

    assert _config_fallback("alerts_enabled") == "true"
    assert _config_fallback("alert_email") == ""


def test_registry_covers_all_schema_fields():
    """Every PATCH-able knob is in the registry (and vice-versa)."""
    from api.admin import _SETTING_DEFS

    schema_fields = set(AdminSettingsUpdate.model_fields)
    assert set(_SETTING_DEFS) == schema_fields


def test_normalise_values():
    """PATCH values are stored as text with bools → "true"/"false" and None → ""."""
    from api.admin import _normalise

    assert _normalise("aqi_warning_threshold", 120) == "120"
    assert _normalise("alerts_enabled", True) == "true"
    assert _normalise("alerts_enabled", False) == "false"
    assert _normalise("alert_email", None) == ""
    assert _normalise("alert_email", "ops@example.com") == "ops@example.com"


# ── AdminSettingsUpdate schema ────────────────────────────────────────────────


def test_admin_settings_update_valid():
    s = AdminSettingsUpdate(aqi_warning_threshold=120, alerts_enabled=False)
    assert s.aqi_warning_threshold == 120
    assert s.alerts_enabled is False
    assert s.aqi_critical_threshold is None  # untouched


def test_admin_settings_update_coerces_numeric_strings():
    """The frontend may send numbers as strings — pydantic coerces them."""
    assert AdminSettingsUpdate(aqi_critical_threshold="180").aqi_critical_threshold == 180


def test_admin_settings_update_allows_null_alert_email():
    """Sending ``alert_email: null`` must be accepted (clears the address)."""
    assert AdminSettingsUpdate(alert_email=None).alert_email is None


@pytest.mark.parametrize(
    "payload",
    [
        {"aqi_warning_threshold": 501},
        {"aqi_critical_threshold": -1},
        {"data_retention_days": 0},
        {"data_retention_days": 4000},
        {"alerts_enabled": "not-a-bool"},
        {"alert_email": "not-an-email"},
        {"bogus_key": 1},
    ],
)
def test_admin_settings_update_rejects_bad_input(payload):
    """Out-of-range, mistyped, and unknown keys all surface as 422 (ValidationError)."""
    with pytest.raises(ValidationError):
        AdminSettingsUpdate(**payload)


# ── Retention wiring (Phase 10 PATCH → data_retention_cleanup) ───────────────


def test_retention_days_setting_wins(db_session):
    from tasks.aggregation import _retention_days

    db_session.add(SystemSetting(key="data_retention_days", value="180"))
    db_session.flush()
    assert _retention_days(db_session) == 180


def test_retention_days_ignores_non_numeric(db_session):
    from tasks.aggregation import _retention_days

    db_session.add(SystemSetting(key="data_retention_days", value="abc"))
    db_session.flush()
    assert _retention_days(db_session) == cfg.DATA_RETENTION_DAYS


def test_data_retention_cleanup_honors_setting_override():
    """A 30-day setting override purges a 400-day-old reading but keeps a fresh one.

    End-to-end against the ``empyrean_test`` DB (composite ``(time, node_id)``
    PK so two rows at distinct timestamps coexist). Cleaned up in ``finally`` so
    the session-wide table stays empty for later tests.
    """
    from tasks.aggregation import data_retention_cleanup

    node_id = "RT-CLEANUP"
    old = datetime.now(timezone.utc) - timedelta(days=400)
    recent = datetime.now(timezone.utc)
    with get_sync_db() as session:
        session.add(Node(node_id=node_id, reading_interval=30, is_active=True))
        session.add(SensorReading(time=old, node_id=node_id, temperature=1.0))
        session.add(SensorReading(time=recent, node_id=node_id, temperature=1.0))
        session.execute(
            pg_insert(SystemSetting)
            .values(key="data_retention_days", value="30")
            .on_conflict_do_update(
                index_elements=[SystemSetting.key], set_={"value": "30"}
            )
        )
        session.commit()
    try:
        assert data_retention_cleanup() == {"deleted": 1}
        with get_sync_db() as session:
            remaining = session.scalar(
                select(func.count())
                .select_from(SensorReading)
                .where(SensorReading.node_id == node_id)
            )
            assert remaining == 1  # only the recent reading survives
    finally:
        with get_sync_db() as session:
            session.execute(
                delete(SensorReading).where(SensorReading.node_id == node_id)
            )
            session.execute(delete(Node).where(Node.node_id == node_id))
            session.execute(
                delete(SystemSetting).where(SystemSetting.key == "data_retention_days")
            )
            session.commit()


# ── Health helpers fail soft ──────────────────────────────────────────────────


def test_check_mqtt_degraded_when_no_client(monkeypatch):
    from api import admin as admin_mod

    monkeypatch.setattr(admin_mod, "get_mqtt_client", lambda: None)
    result = admin_mod._check_mqtt()
    assert result["status"] == "degraded"
    assert "not running" in result["detail"]


def test_check_mqtt_ok_when_connected(monkeypatch):
    from api import admin as admin_mod

    class _Connected:
        def is_connected(self) -> bool:
            return True

    monkeypatch.setattr(admin_mod, "get_mqtt_client", lambda: _Connected())
    assert admin_mod._check_mqtt()["status"] == "ok"


def test_check_redis_degraded_when_down(monkeypatch):
    from api import admin as admin_mod

    class _Down:
        async def ping(self):
            raise ConnectionError("boom")

        async def dbsize(self):
            raise ConnectionError("boom")

        async def info(self, section):
            raise ConnectionError("boom")

    monkeypatch.setattr(admin_mod, "get_redis_client", lambda: _Down())
    check, keys, used = asyncio.run(admin_mod._check_redis())
    assert check["status"] == "degraded"
    assert keys is None and used is None


def test_check_celery_beat_degraded_when_redis_down(monkeypatch):
    from api import admin as admin_mod

    class _Down:
        async def get(self, key):
            raise ConnectionError("boom")

    monkeypatch.setattr(admin_mod, "get_redis_client", lambda: _Down())
    assert asyncio.run(admin_mod._check_celery_beat())["status"] == "degraded"


def test_check_celery_beat_ok_when_fresh(monkeypatch):
    from api import admin as admin_mod

    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    class _Up:
        async def get(self, key):
            return stamp

    monkeypatch.setattr(admin_mod, "get_redis_client", lambda: _Up())
    result = asyncio.run(admin_mod._check_celery_beat())
    assert result["status"] == "ok"
    assert "ago" in result["detail"]


def test_check_celery_beat_degraded_when_stale(monkeypatch):
    from api import admin as admin_mod

    stale = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")

    class _Up:
        async def get(self, key):
            return stale

    monkeypatch.setattr(admin_mod, "get_redis_client", lambda: _Up())
    assert asyncio.run(admin_mod._check_celery_beat())["status"] == "degraded"


def test_check_timescaledb_degraded_when_db_down():
    from api import admin as admin_mod

    result = asyncio.run(admin_mod._check_timescaledb(db_ok=False))
    assert result["status"] == "degraded"
    assert "skipped" in result["detail"]


# ── Beat heartbeat stamp (tasks.alerts) ───────────────────────────────────────


def test_stamp_beat_heartbeat_writes_key(monkeypatch):
    from tasks import alerts as alerts_task

    calls: dict = {}

    class _Fake:
        def set(self, key, value, ex):
            calls["key"] = key
            calls["ex"] = ex
            return True

    monkeypatch.setattr(alerts_task, "get_sync_redis", lambda: _Fake())
    alerts_task._stamp_beat_heartbeat()
    assert calls["key"] == "celery:heartbeat:beat"
    assert calls["ex"] == 3600


def test_stamp_beat_heartbeat_noop_when_redis_unavailable(monkeypatch):
    from tasks import alerts as alerts_task

    monkeypatch.setattr(alerts_task, "get_sync_redis", lambda: None)
    alerts_task._stamp_beat_heartbeat()  # must not raise


def test_stamp_beat_heartbeat_noop_on_write_error(monkeypatch):
    from tasks import alerts as alerts_task

    class _Boom:
        def set(self, key, value, ex):
            raise ConnectionError("boom")

    monkeypatch.setattr(alerts_task, "get_sync_redis", lambda: _Boom())
    alerts_task._stamp_beat_heartbeat()  # must not raise
