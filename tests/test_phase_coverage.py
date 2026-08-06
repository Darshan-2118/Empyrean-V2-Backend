"""
Phase-coverage tests — how much of the pipeline actually works, end to end.

Each test is *cumulative*: ``test_phase_1_to_N`` drives the system from
phase N's entry point down to the database, so a pass means every phase up to
N works *together*. When a new phase lands, append the next cumulative test
here (each test's docstring states exactly what it covers).

The dedicated Testing phase (Phase 13 in ``docs/TODO.md``) is a separate,
exhaustive suite that runs once overall development is complete. This file is
the lightweight "how far have we actually got?" harness — run it after any
phase lands or any fix tier lands to confirm the completed portion still works.

Conventions
-----------
* Redis is OPTIONAL: every cache/rate-limit path fails open, so these tests
  pass whether or not a broker is running (local runs usually have none).
* Async HTTP scenarios run through :func:`_run_async`, which gives each test a
  fresh event loop and disposes the asyncpg pool inside it — the pool binds
  connections to their creation loop, so pooled connections must never outlive
  a test's loop.
* Rows that must be visible to the *async* API are seeded through the sync
  ``get_sync_db()`` pipeline (committed), with unique ids per test so
  committed rows never collide; conftest drops all tables at session end.
* ``/readings/history`` needs TimescaleDB's ``time_bucket()``, which the test
  DB does not install — that endpoint's route-level contract is checked
  instead, and the full aggregation path is left to Phase 13.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app
from api.jwt import create_access_token
from config import get_config
from models import Base, Node, SensorReading, User
from models.base import async_engine, get_sync_db
from models.helpers import hash_password


# ── Async infra ────────────────────────────────────────────────────────────────


def _run_async(coro):
    """Run an async scenario on a fresh loop, then dispose the async pool.

    ``models.base.async_engine`` (asyncpg) binds pooled connections to the loop
    they were created on. Running each scenario via ``asyncio.run`` gives it a
    brand-new loop and disposes the pool *inside* that loop, so no pooled
    connection is ever handed back to a different (dead) loop.
    """

    async def _wrapped():
        try:
            return await coro
        finally:
            await async_engine.dispose()

    return asyncio.run(_wrapped())


def _seed_user_and_node(prefix: str) -> tuple[int, str]:
    """Create (committed) a user + active node via the sync pipeline.

    The API's auth decorators and endpoints read through the async engine, so
    the rows must be committed (not inside the test's rolled-back transaction).
    """
    tag = secrets.token_hex(3)
    username = f"{prefix}_{tag}"
    node_id = f"{prefix.upper()}-{tag}"
    with get_sync_db() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("secret-pass-123", rounds=4),
            role="user",
            is_active=True,
            notification_prefs={},
        )
        session.add(user)
        node = Node(
            node_id=node_id,
            name=f"{prefix} node",
            location_name="Test Lab",
            lat=28.6139,
            lon=77.2090,
            firmware_version="v2.1.0",
            reading_interval=30,
            is_active=True,
        )
        session.add(node)
        session.flush()
        user_id = user.id
    return user_id, node_id


# ── Phase 1 · Scaffolding & core infrastructure ────────────────────────────────


def test_phase_1_scaffolding_and_blueprints():
    """Phase 1: app factory, config, and the blueprint URL map."""
    app = create_app()
    rules = {str(r) for r in app.url_map.iter_rules()}

    assert "/health" in rules
    for fragment in ("/api/v1/auth/", "/api/v1/profile", "/api/v1/readings/", "/api/v1/forecast"):
        assert any(fragment in r for r in rules), f"missing registered route containing {fragment}"
    assert app.config["APP_ENV"] == get_config().APP_ENV


def test_phase_1_health_endpoint():
    """Phase 1: the health endpoint answers over HTTP."""

    async def _scenario():
        client = create_app().test_client()
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "ok"

    _run_async(_scenario())


# ── Phase 2 · Database configuration, models & migrations ──────────────────────


def test_phase_1_to_2_models_and_database(db_session):
    """Phase 2: all 7 documented tables exist and a row round-trips."""
    expected = {
        "users", "refresh_tokens", "nodes", "sensor_readings",
        "hourly_agg", "alerts", "system_settings",
    }
    assert expected.issubset(Base.metadata.tables)

    node = Node(
        node_id="P2-DB-CHECK", name="db check", location_name="Test Lab",
        lat=0.0, lon=0.0, reading_interval=30, is_active=True,
    )
    db_session.add(node)
    db_session.flush()
    assert db_session.get(Node, "P2-DB-CHECK").reading_interval == 30


# ── Phase 3 · Authentication & user management ─────────────────────────────────


def test_phase_1_to_3_auth_flow():
    """Phase 3: register → login → refresh rotation → logout, over HTTP."""

    async def _scenario():
        client = create_app().test_client()
        tag = secrets.token_hex(4)
        username, email = f"phase3_{tag}", f"phase3_{tag}@example.com"
        password = "secret-pass-123"

        # register auto-logs-in and returns a token pair
        resp = await client.post("/api/v1/auth/register", json={
            "username": username, "email": email, "password": password,
        })
        assert resp.status_code == 201
        reg = await resp.get_json()
        assert reg["access_token"] and reg["refresh_token"]
        assert reg["user"]["username"] == username

        # login returns a fresh pair
        resp = await client.post("/api/v1/auth/login", json={
            "username": username, "password": password,
        })
        assert resp.status_code == 201
        login = await resp.get_json()

        # wrong password → RFC 7807 401, indistinguishable from unknown user
        resp = await client.post("/api/v1/auth/login", json={
            "username": username, "password": "wrong-pass",
        })
        assert resp.status_code == 401
        err = await resp.get_json()
        assert err["status"] == 401
        assert err["title"] == "Unauthorized"

        # refresh rotates: a new pair is issued and the old token is revoked
        resp = await client.post("/api/v1/auth/refresh", json={
            "refresh_token": login["refresh_token"],
        })
        assert resp.status_code == 200
        rotated = await resp.get_json()
        assert rotated["access_token"] and rotated["refresh_token"]

        resp = await client.post("/api/v1/auth/refresh", json={
            "refresh_token": login["refresh_token"],  # reuse of a rotated token
        })
        assert resp.status_code == 401

        # logout the live rotated token
        resp = await client.post("/api/v1/auth/logout", json={
            "refresh_token": rotated["refresh_token"],
        })
        assert resp.status_code == 204

    _run_async(_scenario())


# ── Phase 4 · MQTT ingestion layer ─────────────────────────────────────────────


def test_phase_1_to_4_mqtt_ingestion(monkeypatch):
    """Phase 4: topic → validate → dispatch, with the C-1 / H-3 contract intact."""
    import tasks.process_reading as pr
    from mqtt.client import _handle_reading
    from mqtt.validator import validate_reading

    dispatched: list[dict] = []
    monkeypatch.setattr(
        pr.process_reading, "delay", lambda payload: dispatched.append(payload)
    )

    _handle_reading("NODE-P4", json.dumps({
        "time": "2026-08-05T12:00:00Z",
        "pm25": 42.0,
        "temperature": 25.5,
    }))

    assert len(dispatched) == 1
    payload = dispatched[0]
    assert payload["node_id"] == "NODE-P4"           # topic id is authoritative (H-3)
    assert payload["pm25"] == 42.0
    assert payload["time"] == "2026-08-05T12:00:00Z"  # C-1: ISO string, not a datetime

    # Malformed device data is dropped, never dispatched (L-16 / L-17).
    assert validate_reading({"pm25": float("inf")}) is None
    assert validate_reading({"temperature": True}) is None


# ── Phase 5 · Sensor readings API ──────────────────────────────────────────────


def test_phase_1_to_5_readings_api():
    """Phase 5: enriched reading → /readings/latest (JWT + rate-limit wired)."""

    async def _scenario():
        user_id, node_id = _seed_user_and_node("p5")
        with get_sync_db() as session:
            session.add(SensorReading(
                node_id=node_id,
                time=datetime.now(timezone.utc),
                temperature=22.0, humidity=50.0, pm25=35.0,
                aqi=101, aqi_category="Unhealthy for Sensitive Groups",
                fuzzy_score=60.0, is_anomaly=False,
            ))

        client = create_app().test_client()
        headers = {"Authorization": f"Bearer {create_access_token(user_id, 'user')}"}

        resp = await client.get("/api/v1/readings/latest", headers=headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        mine = [r for r in body["readings"] if r["node_id"] == node_id]
        assert len(mine) == 1
        assert mine[0]["aqi"] == 101
        assert mine[0]["time"].endswith("Z")

        # Route-level contract for /history (full time_bucket aggregation needs
        # TimescaleDB, which the test DB does not install).
        resp = await client.get("/api/v1/readings/history?bucket=99m", headers=headers)
        assert resp.status_code == 422

    _run_async(_scenario())


# ── Phase 6 · Tsukamoto fuzzy inference engine ─────────────────────────────────


def test_phase_1_to_6_fuzzy_engine():
    """Phase 6: fuzzy inference end-to-end incl. the 0 °C / NaN regressions."""
    from fuzzy import fuzzy_score, infer

    clean = infer(20.0, 50.0, 10.0)
    assert clean["rules_fired"] >= 1
    assert clean["score"] < 35.0

    heavy = infer(20.0, 50.0, 250.0)
    assert heavy["score"] > 60.0

    # H-1 regression: at 0 °C the Low shoulder must still fire rules for heavy PM.
    cold = infer(0.0, 50.0, 250.0)
    assert cold["rules_fired"] >= 1
    assert cold["score"] > 60.0

    # M-2: NaN/None are rejected at the boundary, never clamped to a domain top.
    for bad in (float("nan"), None):
        with pytest.raises(ValueError):
            infer(bad, 50.0, 10.0)

    # The convenience wrapper used by the Celery task returns just the score.
    assert isinstance(fuzzy_score(20.0, 50.0, 10.0), float)


# ── Phase 7 · Celery tasks (enrichment + forecast) ─────────────────────────────


def test_phase_1_to_7_celery_tasks_and_forecast():
    """Phase 7: enrichment task → DB → linear forecast → forecast API.

    Also asserts the forecast contract: every point's ``time`` is a whole-second
    ISO-8601 string (``^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$``) with no
    microsecond fraction, both from ``generate_forecast`` and over the HTTP
    endpoint.
    """

    async def _scenario():
        from tasks.forecast import generate_forecast
        from tasks.process_reading import process_reading

        user_id, node_id = _seed_user_and_node("p7")

        # Enrich ~40 readings with a rising PM2.5 trend via the task body (no broker).
        base = datetime.now(timezone.utc)
        for i in range(40):
            ts = (base - timedelta(minutes=(40 - i) * 5)).isoformat().replace("+00:00", "Z")
            result = process_reading({
                "node_id": node_id, "time": ts,
                "pm25": 10.0 + i, "temperature": 22.0, "humidity": 50.0,
            })
            assert result["aqi"] is not None      # enrichment computed AQI + fuzzy
            assert result["fuzzy_score"] is not None

        # Enough samples (>= 30) → a real 60-point linear forecast.
        points = generate_forecast(node_id)
        assert len(points) == 60
        assert all("time" in p and "aqi" in p for p in points)
        # Whole-second precision: no fractional seconds, no microsecond dot.
        assert all(re.fullmatch(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", p["time"]) for p in points)
        assert all("." not in p["time"] for p in points)

        # The documented HTTP endpoint serves it.
        client = create_app().test_client()
        resp = await client.get(
            f"/api/v1/forecast?node_id={node_id}",
            headers={"Authorization": f"Bearer {create_access_token(user_id, 'user')}"},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["node_id"] == node_id
        assert body["horizon_minutes"] == 60
        assert len(body["points"]) == 60
        assert all(re.fullmatch(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", p["time"]) for p in body["points"])

    _run_async(_scenario())


# ── Extending (phases 8+) ──────────────────────────────────────────────────────
# When Phase 8 (Nodes API) lands, append `test_phase_1_to_8_nodes_api` here that
# registers / lists / patches a node through the HTTP API. Keep the cumulative
# pattern: it drives phase 8's entry point and relies on phases 1-7 already
# being proven by the tests above. Same for Phase 9 (Alerts/WebSocket), Phase 10
# (Admin), Phase 11 (Export), Phase 12 (Error handling/middleware).


def test_phase_1_to_8_nodes_api():
    """Phase 8: register → list → patch a node over the HTTP API."""

    async def _scenario():
        from api.jwt import create_access_token
        from models import User

        user_id, node_id = _seed_user_and_node("p8")
        admin = User(
            username=f"p8admin_{secrets.token_hex(3)}",
            email=f"p8admin_{secrets.token_hex(3)}@example.com",
            password_hash=hash_password("secret-pass-123", rounds=4),
            role="admin", is_active=True, notification_prefs={},
        )
        with get_sync_db() as session:
            session.add(admin); session.flush(); admin_id = admin.id

        client = create_app().test_client()
        user_headers = {"Authorization": f"Bearer {create_access_token(user_id, 'user')}"}
        admin_headers = {"Authorization": f"Bearer {create_access_token(admin_id, 'admin')}"}

        # list includes the pre-seeded node
        resp = await client.get("/api/v1/nodes", headers=user_headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert any(n["node_id"] == node_id for n in body["nodes"])

        # register a fresh node
        new_id = f"P9-{secrets.token_hex(3).upper()}"
        resp = await client.post("/api/v1/nodes", headers=user_headers, json={
            "node_id": new_id, "name": "Phase8 node", "reading_interval": 60,
        })
        assert resp.status_code == 201
        reg = await resp.get_json()
        assert reg["node_id"] == new_id and reg["reading_interval"] == 60

        # admin patch updates it; no broker → config_pushed false (fail-open)
        resp = await client.patch(f"/api/v1/nodes/{new_id}", headers=admin_headers, json={
            "name": "Renamed", "reading_interval": 120,
        })
        assert resp.status_code == 200
        patched = await resp.get_json()
        assert patched["name"] == "Renamed"
        assert patched["reading_interval"] == 120
        assert patched["config_pushed"] is False

    _run_async(_scenario())
