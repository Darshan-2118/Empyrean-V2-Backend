"""
Alerts blueprint — list and acknowledge threshold-breach alerts.

``GET`` returns **unacknowledged** alerts (``acknowledged_at IS NULL``) with
``limit``/``offset``/``severity`` params. Filtering and pagination run in SQL
(M27) — the old design cached the FULL unacked list under ``alerts:unacked``
and sliced pages in Python, loading thousands of rows per request behind a
noisy node. ``PATCH`` marks an alert acknowledged (idempotent).

Alert *creation* lives in ``tasks.alerts.check_thresholds`` (Celery beat) — not
here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from quart import Blueprint, g, jsonify, request
from sqlalchemy import func, select

from api.jwt import problem_json, jwt_required
from api.rate_limit import rate_limit
from api.schemas import AlertResponse
from models import Alert
from models.base import AsyncSessionLocal

logger = logging.getLogger("empyrean.alerts")

alerts_bp = Blueprint("alerts", __name__)

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_SEVERITIES = {"warning", "critical"}


def _serialise(alert: Alert) -> dict:
    """Convert an ``Alert`` ORM row into an ``AlertResponse`` dict.

    L16: no manual ``float()`` casts — Postgres REAL columns already return
    Python floats via asyncpg, and ``AlertResponse`` coerces its numeric
    fields anyway.
    """
    return AlertResponse(
        alert_id=alert.alert_id,
        node_id=alert.node_id,
        parameter=alert.parameter,
        value=alert.value,
        threshold=alert.threshold,
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
    """Unacknowledged alerts, newest first, with optional filters.

    M27: the severity filter and LIMIT/OFFSET pagination run in SQL. The old
    cache-the-full-list design loaded every unacked alert (10 000+ behind a
    noisy node) into Python on each call just to slice one page out.
    """
    severity = request.args.get("severity")
    if severity is not None and severity not in _SEVERITIES:
        return problem_json(
            422, "Unprocessable Entity", "severity must be 'warning' or 'critical'"
        )

    pagination = _validate_pagination()
    if pagination is None:
        return problem_json(
            422,
            "Unprocessable Entity",
            f"limit must be 1..{_MAX_LIMIT} and offset must be >= 0",
        )
    limit, offset = pagination

    stmt = (
        select(Alert)
        .where(Alert.acknowledged_at.is_(None))
        .order_by(Alert.triggered_at.desc(), Alert.alert_id.desc())
        .limit(limit)
        .offset(offset)
    )
    count_stmt = (
        select(func.count())
        .select_from(Alert)
        .where(Alert.acknowledged_at.is_(None))
    )
    if severity is not None:
        stmt = stmt.where(Alert.severity == severity)
        count_stmt = count_stmt.where(Alert.severity == severity)

    async with AsyncSessionLocal() as session:
        rows = list((await session.execute(stmt)).scalars().all())
        total = int((await session.execute(count_stmt)).scalar_one())

    return jsonify({"alerts": [_serialise(a) for a in rows], "total": total}), 200


@alerts_bp.route("/<int:alert_id>/acknowledge", methods=["PATCH"])
@rate_limit()
@jwt_required
async def acknowledge_alert(alert_id: int):
    """Mark an alert acknowledged (idempotent)."""
    async with AsyncSessionLocal() as session:
        alert = await session.get(Alert, alert_id)
        if alert is None:
            return problem_json(404, "Not Found", "Alert not found")

        if alert.acknowledged_at is None:
            alert.acknowledged_at = datetime.now(timezone.utc)
            alert.acknowledged_by = g.current_user.id
            await session.commit()
            await session.refresh(alert)

        serialised = _serialise(alert)

    return jsonify(serialised), 200