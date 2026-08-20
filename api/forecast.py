"""Forecast blueprint — next-hour AQI predictions per node.

* ``GET /forecast?node_id=<id>``  — ``@jwt_required``, rate-limited.

The 60-minute forecast is served from the ``celery:forecast:{node_id}`` Redis
cache when present, otherwise computed on the fly via
:func:`tasks.forecast.generate_forecast` (the sync call is shimmed with
``asyncio.to_thread`` to keep the event loop responsive). Redis failure
degrades to computing from the DB — we never 500 purely on a cache problem.
"""

from __future__ import annotations

import asyncio
import logging

from quart import Blueprint, jsonify, request
from sqlalchemy import select

from api.cache import cache_get_json
from api.jwt import _problem_json, jwt_required
from api.rate_limit import rate_limit
from api.schemas import ForecastResponse
from models import Node
from models.base import AsyncSessionLocal
from tasks.forecast import generate_forecast

logger = logging.getLogger("empyrean.forecast")

forecast_bp = Blueprint("forecast", __name__)

_FORECAST_CACHE_KEY = "celery:forecast:{node_id}"
HORIZON_MINUTES = 60


@forecast_bp.route("", methods=["GET"])
@rate_limit()
@jwt_required
async def forecast():
    """Return a 60-minute, 1-minute-step AQI forecast for one node.

    Response shape: ``{"node_id", "horizon_minutes": 60,
    "points": [{"time", "aqi"}, ...]}``. Unknown ``node_id`` → 404.
    """
    node_id = (request.args.get("node_id") or "").strip()
    if not node_id:
        return _problem_json(422, "Unprocessable Entity", "node_id is required")

    # Verify an *active* node exists before forecasting, so an unknown or
    # inactive id is a clean 404 (L-29).
    async with AsyncSessionLocal() as session:
        node_exists = await session.scalar(
            select(Node.node_id).where(
                Node.node_id == node_id, Node.is_active.is_(True)
            )
        )
    if node_exists is None:
        return _problem_json(404, "Not Found", f"Unknown or inactive node_id: {node_id}")

    # 1) Redis read-through — cache_get_json degrades to None when Redis is down.
    cached = await cache_get_json(_FORECAST_CACHE_KEY.format(node_id=node_id))
    if cached is not None:
        raw_points = cached
    else:
        # 2) Compute on the fly. generate_forecast is sync; shim it so we do not
        #    block the event loop. It caches its own result, so a cold miss after
        #    this still leaves the forecast warm for the next request.
        try:
            raw_points = await asyncio.to_thread(generate_forecast, node_id)
        except ModuleNotFoundError as exc:
            # sklearn (a hard dependency) is the usual cause. Callers can't fix a
            # 500 — return a clean 503 "forecast unavailable" (#10).
            logger.error("Forecast unavailable for node %s: %s", node_id, exc)
            return _problem_json(
                503,
                "Service Unavailable",
                "Forecast computation is unavailable (missing scikit-learn). "
                "Install scikit-learn to enable forecasts.",
            )
        except Exception:
            logger.exception("Forecast computation failed for node %s", node_id)
            return _problem_json(500, "Internal Server Error", "Forecast failed")

    payload = ForecastResponse(
        node_id=node_id,
        horizon_minutes=HORIZON_MINUTES,
        # raw_points entries are {"time": iso-Z, "aqi": float}; pydantic v2
        # coerces the time string into the datetime ForecastPoint field.
        points=raw_points or [],
    )
    return jsonify(payload.model_dump()), 200