"""
Admin blueprint — system health and system-settings management.

Routes:
* ``GET    /admin/health``   — per-component health (database, TimescaleDB,
  Redis, MQTT, Celery worker/beat) plus DB & Redis size. **Fail-soft:** an
  unreachable component reports ``degraded`` inside the body — the endpoint
  itself always returns ``200`` (matching the liveness ``/health``) so the
  caller, not the status code, interprets component state.
* ``GET    /admin/settings`` — effective system settings. ``system_settings``
  rows win; known knobs fall back to config when a row is absent (``source``
  disambiguates).
* ``PATCH  /admin/settings`` — update the known knobs (AQI thresholds, data
  retention, alerts toggle/email). Upserts rows atomically, validates types and
  that ``aqi_warning_threshold < aqi_critical_threshold``, and stamps
  ``updated_by``. Changes take effect on the next beat/cleanup tick — the
  tasks read ``system_settings`` fresh each run.

All routes are admin-only (``@admin_required``, the Phase 3 RBAC middleware)
and are intentionally **not** rate-limited — privileged, low-volume calls
(matching the ``/profile/*`` precedent).

Errors are RFC 7807 problem JSON.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from quart import Blueprint, g, jsonify
from sqlalchemy import select
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

# Celery worker ping deadline (seconds). Bounded so a dead broker does not hang
# the health endpoint behind a long broadcast wait.
_CELERY_PING_TIMEOUT = 2.0
# A beat heartbeat older than this means scheduled work has stopped firing.
# ``check_thresholds`` ticks every 60s, so 3× the schedule is the tolerance.
_BEAT_MAX_AGE_SECONDS = 180

# The system knobs Phase 10 owns. ``config_key`` is the ``Config`` attribute to
# fall back to when the row is absent; keys without one have a fixed default
# (see :func:`_config_fallback`). Kept in sync with ``AdminSettingsUpdate``.
_SETTING_DEFS: dict[str, dict[str, str | None]] = {
    "aqi_warning_threshold": {
        "description": "AQI value that triggers a warning alert",
        "config_key": "AQI_WARNING_THRESHOLD",
    },
    "aqi_critical_threshold": {
        "description": "AQI value that triggers a critical alert",
        "config_key": "AQI_CRITICAL_THRESHOLD",
    },
    "data_retention_days": {
        "description": "How long raw readings are retained before purging",
        "config_key": "DATA_RETENTION_DAYS",
    },
    "alerts_enabled": {
        "description": "Master toggle for alert generation",
        "config_key": None,
    },
    "alert_email": {
        "description": "Email address that receives critical alerts",
        "config_key": None,
    },
}


def _iso(value) -> str | None:
    """Serialize a ``datetime`` to the repo's ISO-8601-Z contract (or ``None``)."""
    return value.isoformat().replace("+00:00", "Z") if value else None


def _config_fallback(key: str) -> str:
    """Return the effective fallback value for *key* when no DB row exists."""
    meta = _SETTING_DEFS[key]
    cfg_key = meta["config_key"]
    if cfg_key is not None:
        return str(getattr(cfg, cfg_key))
    return "true" if key == "alerts_enabled" else ""


async def _load_settings() -> list[dict]:
    """Return every effective setting as ``{key, value, description, updated_at,
    updated_by, source}`` — registry knobs in order (DB row wins over config
    fallback), then any non-registry rows already in the table.
    """
    stmt = select(SystemSetting).order_by(SystemSetting.key)
    async with AsyncSessionLocal() as session:
        rows = list((await session.execute(stmt)).scalars().all())
    by_key = {s.key: s for s in rows}

    settings: list[dict] = []
    for key, meta in _SETTING_DEFS.items():
        row = by_key.get(key)
        if row is not None:
            settings.append(
                {
                    "key": key,
                    "value": row.value,
                    "description": row.description or meta["description"],
                    "updated_at": _iso(row.updated_at),
                    "updated_by": row.updated_by,
                    "source": "db",
                }
            )
        else:
            settings.append(
                {
                    "key": key,
                    "value": _config_fallback(key),
                    "description": meta["description"],
                    "updated_at": None,
                    "updated_by": None,
                    "source": "config",
                }
            )
    for key, row in by_key.items():
        if key not in _SETTING_DEFS:
            settings.append(
                {
                    "key": key,
                    "value": row.value,
                    "description": row.description,
                    "updated_at": _iso(row.updated_at),
                    "updated_by": row.updated_by,
                    "source": "db",
                }
            )
    return settings


def _normalise(key: str, value) -> str:
    """Convert a validated PATCH value to the text form stored in ``system_settings``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


async def _numeric_setting(session, key: str) -> float | None:
    """Resolve the effective numeric value of *key* (DB row wins, else config)."""
    row = await session.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if row is not None:
        try:
            return float(row.value)
        except ValueError:
            logger.warning("Setting %s=%r is not numeric", key, row.value)
    cfg_key = _SETTING_DEFS.get(key, {}).get("config_key")
    if cfg_key is not None:
        return float(getattr(cfg, cfg_key))
    return None


@admin_bp.route("/settings", methods=["GET"])
@admin_required
async def get_settings():
    """All effective system settings (admin only)."""
    return jsonify({"settings": await _load_settings()}), 200


@admin_bp.route("/settings", methods=["PATCH"])
@admin_required
@validate_body(AdminSettingsUpdate, require_object=True)
async def update_settings():
    """Update known settings (admin only); only provided fields change."""
    data = validated_body()
    updates = data.model_dump(exclude_unset=True)

    async with AsyncSessionLocal() as session:
        # Cross-field guard: the warning threshold must stay below critical.
        # Only enforced when a threshold is actually being changed — patching an
        # unrelated knob must not fail on a pre-existing bad pair.
        if "aqi_warning_threshold" in updates or "aqi_critical_threshold" in updates:
            warning = (
                float(updates["aqi_warning_threshold"])
                if "aqi_warning_threshold" in updates
                else await _numeric_setting(session, "aqi_warning_threshold")
            )
            critical = (
                float(updates["aqi_critical_threshold"])
                if "aqi_critical_threshold" in updates
                else await _numeric_setting(session, "aqi_critical_threshold")
            )
            if warning is not None and critical is not None and warning >= critical:
                return _problem_json(
                    422,
                    "Unprocessable Entity",
                    "aqi_warning_threshold must be less than aqi_critical_threshold",
                )

        for key, value in updates.items():
            text_value = _normalise(key, value)
            # Atomic upsert so a fresh row is created or the existing one updated
            # in one statement (same pattern as tasks/alerts._upsert_alert).
            await session.execute(
                pg_insert(SystemSetting)
                .values(
                    key=key,
                    value=text_value,
                    description=_SETTING_DEFS[key]["description"],
                    updated_by=g.current_user.id,
                )
                .on_conflict_do_update(
                    index_elements=[SystemSetting.key],
                    set_={"value": text_value, "updated_by": g.current_user.id},
                )
            )
        await session.commit()

    return jsonify({"settings": await _load_settings()}), 200


async def _db_info() -> tuple[dict, int | None]:
    """Database reachability + size; fail-soft.

    ``pg_database_size`` is core Postgres (not Timescale), so it works on any
    deployment. Returns ``(check, size_bytes)``.
    """
    from sqlalchemy import text as sa_text

    from models.base import async_engine

    try:
        async with async_engine.connect() as conn:
            await conn.execute(sa_text("SELECT 1"))
            size = (
                await conn.execute(sa_text("SELECT pg_database_size(current_database())"))
            ).scalar()
        return (
            {"status": "ok", "detail": "PostgreSQL reachable"},
            int(size) if size is not None else None,
        )
    except Exception as exc:
        return {"status": "error", "detail": f"unreachable: {exc}"}, None


async def _check_timescaledb(db_ok: bool) -> dict:
    """Confirm ``sensor_readings`` is a real hypertable (or degrade honestly)."""
    if not db_ok:
        return {"status": "degraded", "detail": "skipped (database unreachable)"}

    from sqlalchemy import text as sa_text

    from models.base import async_engine

    try:
        async with async_engine.connect() as conn:
            name = (
                await conn.execute(
                    sa_text(
                        "SELECT hypertable_name FROM timescaledb_information.hypertables "
                        "WHERE hypertable_name = 'sensor_readings'"
                    )
                )
            ).scalar()
    except Exception as exc:
        return {"status": "degraded", "detail": f"TimescaleDB unavailable: {exc}"}

    if name == "sensor_readings":
        return {"status": "ok", "detail": "sensor_readings is a hypertable"}
    return {
        "status": "degraded",
        "detail": "TimescaleDB extension not installed — sensor_readings is a regular table",
    }


async def _check_redis() -> tuple[dict, int | None, int | None]:
    """Redis reachability + size; fail-soft.

    Returns ``(check, keys, used_memory_bytes)``. ``INFO memory``'s
    ``used_memory`` is surfaced as bytes for capacity planning.
    """
    client = get_redis_client()
    if client is None:
        return {"status": "degraded", "detail": "client could not be created"}, None, None
    try:
        pong = await client.ping()
        keys = await client.dbsize()
        info = await client.info("memory")
        used_raw = info.get("used_memory") if isinstance(info, dict) else None
        used = int(used_raw) if used_raw is not None else None
        return (
            {"status": "ok" if pong else "degraded", "detail": f"PING ok, {keys} keys"},
            keys,
            used,
        )
    except Exception as exc:
        return {"status": "degraded", "detail": f"unreachable: {exc}"}, None, None


def _check_mqtt() -> dict:
    """MQTT ingestion client state; fail-soft."""
    client = get_mqtt_client()
    if client is None:
        return {
            "status": "degraded",
            "detail": "ingestion client not running (MQTT_ENABLED unset or startup failed)",
        }
    try:
        connected = client.is_connected()
    except Exception as exc:
        return {"status": "degraded", "detail": f"client error: {exc}"}
    if connected:
        return {"status": "ok", "detail": "connected to broker"}
    return {"status": "degraded", "detail": "not connected to broker"}


async def _check_celery_worker() -> dict:
    """Ping Celery workers; fail-soft.

    ``control.ping`` is a blocking broadcast, so it runs in a thread to keep the
    event loop responsive. A broker that is down surfaces as a ping failure
    rather than a hang (bounded by ``_CELERY_PING_TIMEOUT``).
    """
    import asyncio

    from celery_app import celery_app

    def _ping() -> tuple[list | None, str | None]:
        try:
            return celery_app.control.ping(timeout=_CELERY_PING_TIMEOUT), None
        except Exception as exc:  # noqa: BLE001 - any broker error ⇒ degraded
            return None, str(exc)

    replies, error = await asyncio.to_thread(_ping)
    if error is not None:
        return {"status": "degraded", "detail": f"ping failed: {error}"}
    if not replies:
        return {"status": "degraded", "detail": "no worker responded to ping"}
    hosts = sorted(next(iter(r)) for r in replies if r)
    return {"status": "ok", "detail": f"{len(hosts)} worker(s): {', '.join(hosts)}"}


async def _check_celery_beat() -> dict:
    """Celery-beat liveness via the ``check_thresholds`` heartbeat; fail-soft.

    Beat publishes no heartbeat of its own, so we rely on the task-stamp that
    ``tasks.alerts.check_thresholds`` (the 60s beat task) writes under
    ``BEAT_HEARTBEAT_KEY``. Fresh within ``_BEAT_MAX_AGE_SECONDS`` ⇒ healthy.
    """
    client = get_redis_client()
    if client is None:
        return {"status": "degraded", "detail": "redis unavailable"}
    try:
        raw = await client.get(BEAT_HEARTBEAT_KEY)
    except Exception as exc:
        return {"status": "degraded", "detail": f"redis read failed: {exc}"}
    if not raw:
        return {
            "status": "degraded",
            "detail": "no heartbeat yet — beat has not ticked since startup",
        }
    try:
        stamped = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - stamped).total_seconds()
    except ValueError:
        return {"status": "degraded", "detail": f"unparseable heartbeat {raw!r}"}
    if age <= _BEAT_MAX_AGE_SECONDS:
        return {"status": "ok", "detail": f"last tick {age:.0f}s ago"}
    return {
        "status": "degraded",
        "detail": f"last tick {age:.0f}s ago (> {_BEAT_MAX_AGE_SECONDS}s)",
    }


@admin_bp.route("/health", methods=["GET"])
@admin_required
async def system_health():
    """System health check (admin only) — always 200; read ``status`` in the body."""
    db_check, db_size = await _db_info()
    ts_check = await _check_timescaledb(db_check["status"] == "ok")
    redis_check, redis_keys, redis_used = await _check_redis()
    mqtt_check = _check_mqtt()
    worker_check = await _check_celery_worker()
    beat_check = await _check_celery_beat()

    checks = {
        "database": db_check,
        "timescaledb": ts_check,
        "redis": redis_check,
        "mqtt": mqtt_check,
        "celery_worker": worker_check,
        "celery_beat": beat_check,
    }
    overall = "ok" if all(c["status"] == "ok" for c in checks.values()) else "degraded"

    return (
        jsonify(
            {
                "status": overall,
                "checks": checks,
                "sizes": {
                    "database_bytes": db_size,
                    "redis_keys": redis_keys,
                    "redis_used_memory_bytes": redis_used,
                },
            }
        ),
        200,
    )
