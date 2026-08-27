"""Forecast blueprint — next-hour AQI predictions per node.

* ``GET /forecast?node_id=<id>``  — ``@jwt_required``, rate-limited.

The 60-minute forecast is served from the versioned Redis cache
(``celery:forecast:{node_id}:{model-version}`` — M50) when present,
otherwise computed on the fly via
:func:`tasks.forecast.generate_forecast` (the sync call is shimmed with
``asyncio.to_thread`` to keep the event loop responsive). Redis failure
degrades to computing from the DB — we never 500 purely on a cache problem.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import ValidationError
from quart import Blueprint, jsonify, request
from sqlalchemy import select

from api.cache import cache_get_json
from api.jwt import problem_json, jwt_required
from api.rate_limit import rate_limit
from api.schemas import ForecastResponse
from models import Node
from models.base import AsyncSessionLocal
from tasks.forecast import (
    forecast_cache_key,
    generate_forecast,
    model_cache_key,
    model_version,
)

logger = logging.getLogger("empyrean.forecast")

forecast_bp = Blueprint("forecast", __name__)

HORIZON_MINUTES = 60


def _valid_cached_points(cached) -> list | None:
    """Shape-check a cached forecast before serving it (L45).

    A corrupted Redis blob used to flow straight into ``ForecastResponse``
    and 500 every request until its TTL expired. Anything malformed is
    treated as a cache miss and recomputed instead.
    """
    if not isinstance(cached, list):
        return None
    for point in cached:
        if not isinstance(point, dict) or "time" not in point or "aqi" not in point:
            return None
    return cached


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
        return problem_json(422, "Unprocessable Entity", "node_id is required")

    # Verify an *active* node exists before forecasting, so an unknown or
    # inactive id is a clean 404 (L-29).
    async with AsyncSessionLocal() as session:
        node_exists = await session.scalar(
            select(Node.node_id).where(
                Node.node_id == node_id, Node.is_active.is_(True)
            )
        )
    if node_exists is None:
        return problem_json(404, "Not Found", f"Unknown or inactive node_id: {node_id}")

    # 1) Redis read-through — versioned by the current model (M50). The cache
    #    is only served when it was produced by the model that is current right
    #    now; a missing model, a version mismatch (post-retrain), or a
    #    corrupted blob (L45) all fall through to recomputation. cache_get_json
    #    degrades to None when Redis is down. Key templates live in
    #    tasks.forecast so both sides of the contract stay in sync (L17).
    raw_points: list | None = None
    model_blob = await cache_get_json(model_cache_key(node_id))
    if isinstance(model_blob, dict):
        cached = await cache_get_json(
            forecast_cache_key(node_id, model_version(model_blob))
        )
        raw_points = _valid_cached_points(cached)

    if raw_points is None:
        # 2) Compute on the fly. generate_forecast is sync; shim it so we do not
        #    block the event loop. It caches its own result, so a cold miss after
        #    this still leaves the forecast warm for the next request.
        try:
            raw_points = await asyncio.to_thread(generate_forecast, node_id)
        except ImportError as exc:
            # M29: covers ModuleNotFoundError (missing sklearn, the usual cause)
            # *and* any other ImportError (e.g. a typo'd module path) so a
            # broken forecast stack never surfaces as a raw 500. Callers can't
            # fix either — return a clean 503 "forecast unavailable" (#10).
            logger.error("Forecast unavailable for node %s: %s", node_id, exc)
            return problem_json(
                503,
                "Service Unavailable",
                "Forecast computation is unavailable (missing scikit-learn). "
                "Install scikit-learn to enable forecasts.",
            )
        except Exception:
            logger.exception("Forecast computation failed for node %s", node_id)
            return problem_json(500, "Internal Server Error", "Forecast failed")

    try:
        payload = ForecastResponse(
            node_id=node_id,
            horizon_minutes=HORIZON_MINUTES,
            # raw_points entries are {"time": iso-Z, "aqi": float}; pydantic v2
            # coerces the time string into the datetime ForecastPoint field.
            points=raw_points or [],
        )
    except ValidationError:
        # L45: defense in depth — a cached entry that passed the shape check
        # but still fails the schema (e.g. unparseable timestamps) must not
        # 500 every request until TTL; recompute once from the DB instead.
        logger.warning(
            "Cached forecast for node %s failed schema validation — recomputing",
            node_id,
        )
        try:
            raw_points = await asyncio.to_thread(generate_forecast, node_id)
        except Exception:
            logger.exception("Forecast recomputation failed for node %s", node_id)
            return problem_json(500, "Internal Server Error", "Forecast failed")
        payload = ForecastResponse(
            node_id=node_id,
            horizon_minutes=HORIZON_MINUTES,
            points=raw_points or [],
        )
    return jsonify(payload.model_dump()), 200