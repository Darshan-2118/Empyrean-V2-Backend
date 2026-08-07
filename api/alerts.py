"""
Alerts blueprint — list and acknowledge threshold-breach alerts.

``GET`` returns **unacknowledged** alerts (``acknowledged_at IS NULL``) with
``limit``/``offset``/``severity`` params. The full unacknowledged list is cached
under ``alerts:unacked`` (TTL 30s); filters and pagination are applied in-memory
after the cache read so the cache key never varies with query params. A 30s TTL
bounds staleness for newly-created alerts (the WebSocket channel covers the
real-time path). ``PATCH`` marks an alert acknowledged (idempotent) and
invalidates the cache.

Alert *creation* lives in ``tasks.alerts.check_thresholds`` (Celery beat) — not
here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from quart import Blueprint, g, jsonify, request
from sqlalchemy import select

from api.cache import cache_delete, cache_get_json, cache_set_json
from api.jwt import _problem_json, jwt_required
from api.rate_limit import rate_limit
from api.schemas import AlertResponse
from models import Alert
from models.base import AsyncSessionLocal

logger = logging.getLogger("empyrean.alerts")

alerts_bp = Blueprint("alerts", __name__)

_ALERTS_CACHE_KEY = "alerts:unacked"
_ALERTS_CACHE_TTL = 30  # docs/database.md contract
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_SEVERITIES = {"warning", "critical"}


def _serialise(alert: Alert) -> dict:
    """Convert an ``Alert`` ORM row into an ``AlertResponse`` dict.

    ``value``/``threshold`` are Postgres REAL (may surface as ``float`` or
    ``Decimal``) — coerce to ``float``.
    """
    return AlertResponse(
        alert_id=alert.alert_id,
        node_id=alert.node_id,
        parameter=alert.parameter,
        value=float(alert.value),
        threshold=float(alert.threshold),
        severity=alert.severity,
        message=alert.message,
        triggered_at=alert.triggered_at,
        acknowledged_at=alert.acknowledged_at,
        acknowledged_by=alert.acknowledged_by,
    ).model_dump()


def _validate_pagination() -> tuple[int, int] | None:
    """Parse+validate ``limit``/``offset``; return ``None`` on 422 (already sent)."""
    raw_limit = request.args.get("limit", str(_DEFAULT_LIMIT))
    try:
        limit = int(raw_limit)
    except ValueError:
        return None  # handled below
    if not 1 <= limit <= _MAX_LIMIT:
        return None
    raw_offset = request.args.get("offset", "0")
    try:
        offset = int(raw_offset)
    except ValueError:
        return None
    if offset < 0:
        return None
    return limit, offset


@alerts_bp.route("", methods=["GET"])
@rate_limit()
@jwt_required
async def list_alerts():
    """Unacknowledged alerts, newest first, with optional filters."""
    severity = request.args.get("severity")
    if severity is not None and severity not in _SEVERITIES:
        return _problem_json(
            422, "Unprocessable Entity", "severity must be 'warning' or 'critical'"
        )

    pagination = _validate_pagination()
    if pagination is None:
        return _problem_json(
            422,
            "Unprocessable Entity",
            f"limit must be 1..{_MAX_LIMIT} and offset must be >= 0",
        )
    limit, offset = pagination

    cached = await cache_get_json(_ALERTS_CACHE_KEY)
    if cached is None:
        stmt = (
            select(Alert)
            .where(Alert.acknowledged_at.is_(None))
            .order_by(Alert.triggered_at.desc(), Alert.alert_id.desc())
        )
        async with AsyncSessionLocal() as session:
            rows = list((await session.execute(stmt)).scalars().all())
        cached = [_serialise(a) for a in rows]
        await cache_set_json(_ALERTS_CACHE_KEY, cached, _ALERTS_CACHE_TTL)

    if severity is not None:
        cached = [a for a in cached if a["severity"] == severity]

    total = len(cached)
    page = cached[offset : offset + limit]
    return jsonify({"alerts": page, "total": total}), 200


@alerts_bp.route("/<int:alert_id>/acknowledge", methods=["PATCH"])
@rate_limit()
@jwt_required
async def acknowledge_alert(alert_id: int):
    """Mark an alert acknowledged (idempotent); invalidate the alerts cache."""
    async with AsyncSessionLocal() as session:
        alert = await session.get(Alert, alert_id)
        if alert is None:
            return _problem_json(404, "Not Found", "Alert not found")

        if alert.acknowledged_at is None:
            alert.acknowledged_at = datetime.now(timezone.utc)
            alert.acknowledged_by = g.current_user.id
            await session.commit()
            await session.refresh(alert)

        serialised = _serialise(alert)

    await cache_delete(_ALERTS_CACHE_KEY)
    return jsonify(serialised), 200