"""Admin blueprint — health diagnostics and runtime system settings.

All endpoints require admin privileges (``@admin_required``).
Routes:
* ``GET /admin/health``    — per-component fail-soft diagnostic health report.
* ``GET /admin/settings``  — effective system settings with config fallbacks.
* ``PATCH /admin/settings``— update configurable knobs (persisted in ``system_settings``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from quart import Blueprint, g, jsonify, request
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.cache import get_client as get_redis_client
from api.jwt import _problem_json, admin_required
from api.schemas import AdminSettingsUpdate
from api.validation import validate_body, validated_body
from config import get_config
from models import SystemSetting
from models.base import AsyncSessionLocal
from mqtt.registry import get_client as get_mqtt_client
from tasks._redis import BEAT_HEARTBEAT_KEY

logger = logging.getLogger("empyrean.admin")

admin_bp = Blueprint("admin", __name__)

cfg = get_config()

_SETTING_DEFS: dict[str, dict[str, Any]] = {
    "aqi_warning_threshold": {
        "description": "AQI value that triggers a warning alert",
        "fallback": lambda: str(get_config().AQI_WARNING_THRESHOLD),
    },
    "aqi_critical_threshold": {
        "description": "AQI value that triggers a critical alert",
        "fallback": lambda: str(get_config().AQI_CRITICAL_THRESHOLD),
    },
    "data_retention_days": {
        "description": "How long raw readings are retained before purging",
        "fallback": lambda: str(get_config().DATA_RETENTION_DAYS),
    },
    "alerts_enabled": {
        "description": "Master toggle for alert generation",
        "fallback": lambda: "true",
    },
    "alert_email": {
        "description": "Email address that receives critical alerts",
        "fallback": lambda: "",
    },
}


def _config_fallback(key: str) -> str:
    """Return the configuration fallback string for the given setting key."""
    definition = _SETTING_DEFS.get(key)
    if definition:
        fallback_fn = definition["fallback"]
        return fallback_fn()
    return ""


def _normalise(key: str, value: Any) -> str:
    """Normalise setting values for textual storage in system_settings."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def _load_settings() -> list[dict[str, Any]]:
    """Load all settings from DB, falling back to config for missing registry keys."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SystemSetting))
        db_settings = {s.key: s for s in result.scalars().all()}

    output: list[dict[str, Any]] = []

    # First add all known registry keys
    for key, defn in _SETTING_DEFS.items():
        if key in db_settings:
            s = db_settings[key]
            output.append({
                "key": key,
                "value": s.value,
                "description": s.description or defn["description"],
                "updated_at": s.updated_at.isoformat().replace("+00:00", "Z") if s.updated_at else None,
                "updated_by": s.updated_by,
                "source": "db",
            })
        else:
            output.append({
                "key": key,
                "value": _config_fallback(key),
                "description": defn["description"],
                "updated_at": None,
                "updated_by": None,
                "source": "config",
            })

    # Add any extra DB rows that are not in _SETTING_DEFS
    for key, s in db_settings.items():
        if key not in _SETTING_DEFS:
            output.append({
                "key": key,
                "value": s.value,
                "description": s.description,
                "updated_at": s.updated_at.isoformat().replace("+00:00", "Z") if s.updated_at else None,
                "updated_by": s.updated_by,
                "source": "db",
            })

    return output


# ── Health Diagnostic Helpers ──────────────────────────────────────────────────


def _check_mqtt() -> dict[str, str]:
    """Check MQTT ingestion client status."""
    client = get_mqtt_client()
    if client is None:
        return {
            "status": "degraded",
            "detail": "ingestion client not running (MQTT_ENABLED unset or startup failed)",
        }
    if hasattr(client, "is_connected") and client.is_connected():
        return {"status": "ok", "detail": "connected to broker"}
    return {"status": "degraded", "detail": "ingestion client not connected"}


async def _check_redis() -> tuple[dict[str, str], int | None, int | None]:
    """Check Redis reachability, keys count, and memory usage."""
    client = get_redis_client()
    if client is None:
        return {"status": "degraded", "detail": "Redis client not available"}, None, None

    try:
        await client.ping()
        keys = await client.dbsize()
        info = await client.info("memory")
        used_mem = info.get("used_memory", 0) if isinstance(info, dict) else 0
        return (
            {"status": "ok", "detail": f"PING ok, {keys} keys"},
            keys,
            used_mem,
        )
    except Exception as exc:
        return {"status": "degraded", "detail": f"Redis unreachable: {exc}"}, None, None


async def _check_celery_worker() -> dict[str, str]:
    """Ping Celery workers to verify availability."""
    try:
        from celery_app import celery_app

        def _ping():
            return celery_app.control.ping(timeout=2.0)

        replies = await asyncio.to_thread(_ping)
        if replies:
            return {"status": "ok", "detail": f"{len(replies)} worker(s) online"}
        return {"status": "degraded", "detail": "no worker responded to ping"}
    except Exception as exc:
        return {"status": "degraded", "detail": f"celery control error: {exc}"}


async def _check_celery_beat() -> dict[str, str]:
    """Check freshness of the Celery beat heartbeat key in Redis."""
    client = get_redis_client()
    if client is None:
        return {"status": "degraded", "detail": "Redis unavailable for beat check"}

    try:
        stamp_str = await client.get(BEAT_HEARTBEAT_KEY)
        if not stamp_str:
            return {
                "status": "degraded",
                "detail": "no heartbeat yet — beat has not ticked since startup",
            }

        stamp = datetime.fromisoformat(stamp_str.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if age <= 180:
            return {"status": "ok", "detail": f"heartbeat fresh ({age:.0f}s ago)"}
        return {"status": "degraded", "detail": f"heartbeat stale ({age:.0f}s ago > 180s)"}
    except Exception as exc:
        return {"status": "degraded", "detail": f"failed to check beat heartbeat: {exc}"}


async def _check_timescaledb(db_ok: bool) -> dict[str, str]:
    """Check if sensor_readings is a hypertable."""
    if not db_ok:
        return {"status": "degraded", "detail": "skipped (database connection failed)"}

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT 1 FROM timescaledb_information.hypertables "
                    "WHERE hypertable_name = 'sensor_readings'"
                )
            )
            if result.scalar():
                return {"status": "ok", "detail": "sensor_readings is a hypertable"}
            return {"status": "degraded", "detail": "sensor_readings is not a hypertable"}
    except Exception:
        # Check alternative catalog or report degraded
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        "SELECT 1 FROM _timescaledb_catalog.hypertable "
                        "WHERE table_name = 'sensor_readings'"
                    )
                )
                if result.scalar():
                    return {"status": "ok", "detail": "sensor_readings is a hypertable"}
        except Exception:
            pass
        return {"status": "degraded", "detail": "timescaledb extension absent or unconfigured"}


async def _db_info() -> tuple[dict[str, str], int | None, bool]:
    """Check DB connection and fetch database size in bytes."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_size = await session.scalar(text("SELECT pg_database_size(current_database())"))
            return {"status": "ok", "detail": "PostgreSQL reachable"}, db_size, True
    except Exception as exc:
        return {"status": "error", "detail": f"PostgreSQL error: {exc}"}, None, False


# ── Routes ─────────────────────────────────────────────────────────────────────


@admin_bp.route("/settings", methods=["GET"])
@admin_required
async def get_settings():
    """Retrieve all effective system settings."""
    settings = await _load_settings()
    return jsonify({"settings": settings}), 200


@admin_bp.route("/settings", methods=["PATCH"])
@admin_required
@validate_body(AdminSettingsUpdate, require_object=True)
async def update_settings():
    """Update configurable knobs; values are persisted in system_settings."""
    data = validated_body()
    updates = data.model_dump(exclude_unset=True)

    if not updates:
        return jsonify({"settings": await _load_settings()}), 200

    # Cross-field validation: aqi_warning_threshold must be < aqi_critical_threshold
    current_settings = {s["key"]: s["value"] for s in await _load_settings()}

    warning = updates.get(
        "aqi_warning_threshold",
        int(current_settings.get("aqi_warning_threshold", cfg.AQI_WARNING_THRESHOLD)),
    )
    critical = updates.get(
        "aqi_critical_threshold",
        int(current_settings.get("aqi_critical_threshold", cfg.AQI_CRITICAL_THRESHOLD)),
    )

    if warning is not None and critical is not None and int(warning) >= int(critical):
        return _problem_json(
            422,
            "Unprocessable Entity",
            "aqi_warning_threshold must stay < aqi_critical_threshold",
        )

    admin_id = g.current_user.id

    async with AsyncSessionLocal() as session:
        for key, value in updates.items():
            text_value = _normalise(key, value)
            description = _SETTING_DEFS.get(key, {}).get("description")
            stmt = (
                pg_insert(SystemSetting)
                .values(
                    key=key,
                    value=text_value,
                    description=description,
                    updated_by=admin_id,
                )
                .on_conflict_do_update(
                    index_elements=[SystemSetting.key],
                    set_={
                        "value": text_value,
                        "updated_by": admin_id,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
            )
            await session.execute(stmt)
        await session.commit()

    return jsonify({"settings": await _load_settings()}), 200


@admin_bp.route("/health", methods=["GET"])
@admin_required
async def health():
    """Fail-soft per-component health and size diagnostics."""
    db_check, db_bytes, db_ok = await _db_info()
    timescale_check = await _check_timescaledb(db_ok)
    redis_check, redis_keys, redis_used_mem = await _check_redis()
    mqtt_check = _check_mqtt()
    worker_check = await _check_celery_worker()
    beat_check = await _check_celery_beat()

    checks = {
        "database": db_check,
        "timescaledb": timescale_check,
        "redis": redis_check,
        "mqtt": mqtt_check,
        "celery_worker": worker_check,
        "celery_beat": beat_check,
    }

    all_ok = all(c["status"] == "ok" for c in checks.values())
    overall_status = "ok" if all_ok else "degraded"

    sizes = {
        "database_bytes": db_bytes,
        "redis_keys": redis_keys,
        "redis_used_memory_bytes": redis_used_mem,
    }

    return jsonify({
        "status": overall_status,
        "checks": checks,
        "sizes": sizes,
    }), 200