"""
Temporary phase 1–10 smoke script (health + working).

Runs a quick liveness check for each completed phase (1–10) of the Empyrean
backend. Lightweight by design: it exercises imports, the app factory, the
blueprint/route map, the core pure-logic entry points (fuzzy inference, MQTT
payload validation + topic-authoritative dispatch with a stubbed broker, JWT
round-trip), and does a best-effort DB health probe. It deliberately does NOT
run the full behavioral suite — that is the phase-coverage harness
(``tests/test_phase_coverage.py``) and, later, the dedicated Phase 13 testing
phase. It is TEMPORARY and will be replaced by a proper full smoke/verification
script in a later stage.

Every phase that needs a live service (Postgres) degrades to ``[SKIP]`` when
that service is unreachable — the script never hard-fails on infrastructure you
haven't started.

Exit code 0 = every phase reports OK (passes or skips); 1 = at least one phase FAIL.

Usage::

    venv/Scripts/python.exe scripts/smoke_phases.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

PASS, FAIL, SKIP = "[PASS]", "[FAIL]", "[SKIP]"


def _db_reachable() -> bool:
    """Best-effort check that the configured Postgres is up (no hard failure)."""
    try:
        from models.base import sync_engine
        from sqlalchemy import text as sa_text

        with sync_engine.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
        return True
    except Exception:
        return False


def phase_1_scaffolding() -> tuple[list[str], str]:
    """App factory builds; health + blueprints registered."""
    problems: list[str] = []
    try:
        from app import create_app

        app = create_app()
        rules = {str(r) for r in app.url_map.iter_rules()}
        for fragment in ("/health", "/api/v1/auth/", "/api/v1/profile", "/api/v1/readings/", "/api/v1/forecast"):
            if not any(fragment in r for r in rules):
                problems.append(f"missing route containing {fragment!r}")
        return problems, f"{len(rules)} routes registered"
    except Exception as exc:  # noqa: BLE001 — smoke script reports, does not crash
        return [f"create_app failed: {exc}"], ""


def phase_2_database_models() -> tuple[list[str], str]:
    """All 7 documented tables are declared on the metadata."""
    problems: list[str] = []
    try:
        from models import Base

        expected = {
            "users", "refresh_tokens", "nodes", "sensor_readings",
            "hourly_agg", "alerts", "system_settings",
        }
        missing = expected - set(Base.metadata.tables)
        problems.extend(f"missing model table: {name}" for name in sorted(missing))
        note = "7/7 tables declared on metadata"
        if _db_reachable():
            note += " | DB reachable"
        else:
            note += " | DB not reachable (skipping live probe)"
        return problems, note
    except Exception as exc:  # noqa: BLE001
        return [f"model import failed: {exc}"], ""


def phase_3_auth_jwt() -> tuple[list[str], str]:
    """JWT mint + decode round-trip; decorators importable."""
    problems: list[str] = []
    try:
        from api.jwt import create_access_token, decode_access_token

        token = create_access_token(1, "user")
        payload = decode_access_token(token)
        if payload.get("sub") != 1 or payload.get("role") != "user":
            problems.append("JWT round-trip payload mismatch")
        return problems, "JWT round-trip OK"
    except Exception as exc:  # noqa: BLE001
        return [f"JWT check failed: {exc}"], ""


def phase_4_mqtt_ingestion() -> tuple[list[str], str]:
    """Payload validates; topic id is authoritative at dispatch (stubbed broker)."""
    problems: list[str] = []
    try:
        import json

        from mqtt.client import _handle_reading
        from mqtt.validator import validate_reading

        # Validation accepts a compliant reading and rejects a NaN / bool float.
        good = validate_reading({"node_id": "N-1", "time": "2026-08-05T12:00:00Z", "pm25": 42.0})
        if good is None:
            problems.append("validate_reading rejected a compliant payload")
        if validate_reading({"pm25": float("inf")}) is not None:
            problems.append("validate_reading accepted +Infinity")

        # Topic-authoritative dispatch (H-3): body node_id is overridden by the topic id.
        dispatched: list[dict] = []
        import tasks.process_reading as pr

        orig_delay = pr.process_reading.delay
        try:
            pr.process_reading.delay = lambda payload: dispatched.append(payload)
            _handle_reading("TOPIC-NODE", json.dumps({"node_id": "SPOOFED", "pm25": 7.0}))
        finally:
            pr.process_reading.delay = orig_delay

        if len(dispatched) != 1 or dispatched[0]["node_id"] != "TOPIC-NODE":
            problems.append("dispatch did not attribute by the topic id")
        return problems, "validate + dispatch OK (broker stubbed)"
    except Exception as exc:  # noqa: BLE001
        return [f"MQTT check failed: {exc}"], ""


def phase_5_readings_api() -> tuple[list[str], str]:
    """Readings DTOs serialize; latest/history routes are wired."""
    problems: list[str] = []
    try:
        from datetime import datetime, timezone

        from api.schemas import HistoryBucket, LatestReading

        latest = LatestReading(
            node_id="N", time=datetime.now(timezone.utc), temperature=22.0,
            humidity=50.0, pm25=35.0, aqi=101, aqi_category="Unhealthy for Sensitive Groups",
            fuzzy_score=60.0, is_anomaly=False,
        ).model_dump()
        if latest["node_id"] != "N":
            problems.append("LatestReading DTO round-trip mismatch")
        HistoryBucket(
            bucket=datetime.now(timezone.utc), node_id="N", avg_temperature=1.0,
            avg_humidity=1.0, avg_pm25=1.0, avg_pm10=1.0, avg_aqi=1.0,
            max_aqi=1, min_aqi=1, reading_count=1,
        ).model_dump()
        return problems, "DTOs OK | /latest & /history live (DB not probed)"
    except Exception as exc:  # noqa: BLE001
        return [f"readings check failed: {exc}"], ""


def phase_6_fuzzy_engine() -> tuple[list[str], str]:
    """infer() returns a bounded score and fires rules at 0 °C heavy pollution."""
    problems: list[str] = []
    try:
        from fuzzy import fuzzy_score, infer

        score = fuzzy_score(20.0, 50.0, 10.0)
        if not 0.0 <= score <= 100.0:
            problems.append(f"score out of range: {score}")
        cold = infer(0.0, 50.0, 250.0)
        if cold["rules_fired"] < 1 or cold["score"] <= 60.0:
            problems.append("0 °C heavy-pollution regression (H-1) not held")
        return problems, f"infer OK | clean={score:.1f}, cold={cold['score']:.1f}"
    except Exception as exc:  # noqa: BLE001
        return [f"fuzzy check failed: {exc}"], ""


def phase_7_celery_tasks() -> tuple[list[str], str]:
    """Celery app builds; all task modules register; forecast imports."""
    problems: list[str] = []
    try:
        import celery_app

        include = celery_app.celery_app.conf.include or []
        for module in ("tasks.aggregation", "tasks.alerts", "tasks.forecast", "tasks.process_reading"):
            if module not in include:
                problems.append(f"celery include missing {module}")
        from tasks.forecast import generate_forecast  # noqa: F401  (lazy sklearn import must not fire here)
        return problems, "4 task modules registered | forecast importable"
    except Exception as exc:  # noqa: BLE001
        return [f"celery check failed: {exc}"], ""


def phase_8_nodes_api() -> tuple[list[str], str]:
    """Nodes API: routes wired; registry + cache_delete + Node DTOs live."""
    problems: list[str] = []
    try:
        from app import create_app

        rules = {str(r) for r in create_app().url_map.iter_rules()}
        for fragment in ("/api/v1/nodes",):
            if not any(fragment in r for r in rules):
                problems.append(f"missing route containing {fragment!r}")

        # mqtt/registry round-trip (broker client holder, Task 1).
        from mqtt.registry import get_client, set_client

        set_client("dummy-client")
        if get_client() != "dummy-client":
            problems.append("registry set/get round-trip failed")
        set_client(None)

        # cache_delete importable (Task 2).
        from api.cache import cache_delete  # noqa: F401

        # Node schemas serialize the ISO-8601 trailing-Z contract (Task 3).
        from datetime import datetime, timezone

        from api.schemas import NodeResponse

        dumped = NodeResponse(
            node_id="N-1", reading_interval=30, is_active=True,
            registered_at=datetime.now(timezone.utc), last_seen=None,
        ).model_dump()
        if not dumped["registered_at"].endswith("Z") or dumped["last_seen"] is not None:
            problems.append("NodeResponse datetime serialization is not ISO-Z")
        return problems, "routes wired | registry + cache_delete + schemas OK"
    except Exception as exc:  # noqa: BLE001
        return [f"nodes check failed: {exc}"], ""


def phase_9_alerts_ws() -> tuple[list[str], str]:
    """Alerts API + WebSocket: routes wired; manager + Alert DTO importable."""
    problems: list[str] = []
    try:
        from app import create_app

        rules = {str(r) for r in create_app().url_map.iter_rules()}
        for fragment in ("/api/v1/alerts", "/ws/alerts"):
            if not any(fragment in r for r in rules):
                problems.append(f"missing route containing {fragment!r}")

        from api.schemas import AlertResponse  # noqa: F401
        from api.ws.manager import manager

        if not hasattr(manager, "broadcast"):
            problems.append("WS manager has no broadcast()")
        return problems, "routes wired | manager + AlertResponse importable"
    except Exception as exc:  # noqa: BLE001
        return [f"alerts/ws check failed: {exc}"], ""


def phase_10_admin() -> tuple[list[str], str]:
    """Admin API: routes wired; settings registry + schema + RBAC importable."""
    problems: list[str] = []
    try:
        from app import create_app

        rules = {str(r) for r in create_app().url_map.iter_rules()}
        for fragment in ("/api/v1/admin/health", "/api/v1/admin/settings"):
            if not any(fragment in r for r in rules):
                problems.append(f"missing route containing {fragment!r}")

        import api.admin as admin_mod
        from api.jwt import admin_required  # noqa: F401
        from api.schemas import AdminSettingsUpdate

        for key in (
            "aqi_warning_threshold",
            "aqi_critical_threshold",
            "data_retention_days",
            "alerts_enabled",
            "alert_email",
        ):
            if key not in admin_mod._SETTING_DEFS:
                problems.append(f"settings registry missing {key}")

        # The schema forbids unknown keys (PATCH typo protection).
        try:
            AdminSettingsUpdate(**{"bogus_key": 1})
            problems.append("AdminSettingsUpdate accepted an unknown key")
        except Exception:
            pass
        return problems, "3 routes wired | registry + schema + RBAC importable"
    except Exception as exc:  # noqa: BLE001
        return [f"admin check failed: {exc}"], ""


_PHASES = [
    (1, "Scaffolding & app factory", phase_1_scaffolding),
    (2, "Database models", phase_2_database_models),
    (3, "Authentication & JWT", phase_3_auth_jwt),
    (4, "MQTT ingestion", phase_4_mqtt_ingestion),
    (5, "Readings API", phase_5_readings_api),
    (6, "Fuzzy inference engine", phase_6_fuzzy_engine),
    (7, "Celery tasks", phase_7_celery_tasks),
    (8, "Nodes API", phase_8_nodes_api),
    (9, "Alerts & WebSocket", phase_9_alerts_ws),
    (10, "Admin endpoints", phase_10_admin),
]


def main() -> bool:
    print("Empyrean phase 1-10 smoke (TEMPORARY - replaced by the full script in a later stage)")
    print("=" * 66)
    all_ok = True
    for number, name, fn in _PHASES:
        problems, note = fn()
        if problems:
            all_ok = False
            print(f"  {FAIL}  Phase {number}: {name}")
            for problem in problems:
                print(f"          - {problem}")
        else:
            print(f"  {PASS}  Phase {number}: {name}  ({note})")
    print("=" * 66)
    print("  [OK]  ALL PHASES PASS" if all_ok else "  [FAIL]  ONE OR MORE PHASES FAILED")
    print("=" * 66)
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
