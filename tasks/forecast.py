"""Forecast-related Celery tasks.

``retrain_model`` fits a simple linear AQI trend per active node (minutes →
AQI) and stashes ``{"slope", "intercept", "trained_at"}`` in Redis under
``forecast:model:{node_id}``. ``generate_forecast(node_id)`` is a plain,
non-task helper (callable from the API and reverifiable as a task) that turns
that model into a 60-minute-ahead, 1-minute-step AQI forecast.

Design notes:
    * The 7-day history gate (>= 30 non-null readings) prevents fitting on
      trivial/no data.
    * A linear fit on UTC epoch-seconds is intentionally simple — this is a
      short-horizon live-product forecast, not a production ML pipeline.
    * ``sklearn`` is imported lazily inside the training path so importing
      this module (and the API that depends on it) never requires sklearn.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from celery_app import celery_app
from models import Node, SensorReading
from models.base import get_sync_db
from tasks._redis import get_sync_redis

logger = logging.getLogger("empyrean.tasks.forecast")

# Prediction horizon (minutes) and step (1 minute → 60 points).
FORECAST_HORIZON_MINUTES = 60
FORECAST_STEP_SECONDS = 60
# Minimum non-null readings in the last 7 days to train a model.
_MIN_TRAIN_SAMPLES = 30
_TRAIN_WINDOW_DAYS = 7

# Redis key contracts (docs/database.md) + TTL 3600s for both model & forecast.
_MODEL_KEY_TTL = 3600
_FORECAST_KEY_TTL = 3600


# ── Data retrieval / model helpers ─────────────────────────────────────────────


def _training_points(node_id: str) -> list[tuple[float, float]]:
    """Return ``(epoch_seconds, aqi)`` pairs for one node over the last 7 days."""
    since = datetime.now(timezone.utc) - timedelta(days=_TRAIN_WINDOW_DAYS)
    stmt = (
        select(SensorReading.time, SensorReading.aqi)
        .where(
            SensorReading.node_id == node_id,
            SensorReading.time >= since,
            SensorReading.aqi.is_not(None),
        )
        .order_by(SensorReading.time.asc())
    )
    with get_sync_db() as session:
        rows = session.execute(stmt).all()
    return [(t.timestamp(), float(aqi)) for t, aqi in rows]


def _fit_model(points: list[tuple[float, float]]) -> dict | None:
    """Fit ``LinearRegression`` on pills-epoch-seconds → aqi.

    Returns ``{"slope", "intercept", "trained_at"}`` or ``None`` if there is
    not enough data to train on. ``sklearn`` is imported lazily here.
    """
    if len(points) < _MIN_TRAIN_SAMPLES:
        return None

    from sklearn.linear_model import LinearRegression

    model = LinearRegression()
    xs = [[x] for x, _ in points]
    ys = [y for _, y in points]
    model.fit(xs, ys)

    return {
        "slope": float(model.coef_[0]),
        "intercept": float(model.intercept_),
        "trained_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# ── Redis helpers (sync, Celery-side) ──────────────────────────────────────────


def _redis():
    """Return the shared sync Redis client, creating it lazily.

    Delegates to :func:`tasks._redis.get_sync_redis` — one client per worker
    process, reused for all calls; ``None`` after a failed construction so
    callers degrade gracefully.
    """
    return get_sync_redis()


def _valid_model(model: dict | None) -> bool:
    """True when *model* is a usable ``{slope, intercept, ...}`` dict (L-10).

    Rejects non-dicts and any model whose ``slope``/``intercept`` is not a
    finite int/float — a bogus blob would otherwise reach
    ``slope * ts.timestamp() + intercept`` and 500 the API route.
    """
    if not isinstance(model, dict):
        return False
    for key in ("slope", "intercept"):
        v = model.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return False
    return True


def _get_model(node_id: str) -> dict | None:
    """Read ``forecast:model:{node_id}`` as a dict, ``None`` on miss/down/bad."""
    try:
        raw = _redis().get(f"forecast:model:{node_id}")
    except Exception:
        logger.warning("Redis read failed for forecast:model:%s — missing", node_id)
        return None
    if not raw:
        return None
    try:
        model = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Stored model for %s is invalid JSON — ignoring", node_id)
        return None
    if not _valid_model(model):
        logger.warning("Stored model for %s has invalid values — ignoring", node_id)
        return None
    return model


def _cache_forecast(node_id: str, points: list[dict]) -> None:
    """Cache the forecast as ``celery:forecast:{node_id}``. Redis failure = log only."""
    try:
        _redis().setex(
            f"celery:forecast:{node_id}", _FORECAST_KEY_TTL, json.dumps(points)
        )
    except Exception:
        logger.warning("Redis forecast cache write failed for node %s", node_id)


# ── Public API ─────────────────────────────────────────────────────────────────


@celery_app.task
def retrain_model() -> dict:
    """Retrain the AQI forecast model for every active, data-rich node.

    Fits each node's last-7-days (time, aqi) readings (>= 30 samples) with a
    linear regression and stores ``{"slope", "intercept", "trained_at"}`` in
    ``forecast:model:{node_id}`` (TTL 3600s).

    Returns ``{"models": n}`` — nodes whose model was (re)trained.
    """
    with get_sync_db() as session:
        active_nodes = session.scalars(
            select(Node.node_id).where(Node.is_active.is_(True))
        ).all()

    trained = 0
    for node_id in active_nodes:
        model = _fit_model(_training_points(node_id))
        if model is None:
            logger.info("Insufficient data to train forecast for node %s", node_id)
            continue
        try:
            _redis().setex(
                f"forecast:model:{node_id}", _MODEL_KEY_TTL, json.dumps(model)
            )
            trained += 1
        except Exception:
            logger.warning("Redis write failed for model of node %s — skipping", node_id)
            continue
        # L-11: invalidate any served forecast so a retrain isn't masked by the
        # stale 1h cache. Fail-soft on its own so a delete error never masks a
        # successful model write or under-counts ``trained``.
        try:
            _redis().delete(f"celery:forecast:{node_id}")
        except Exception:
            logger.warning("Redis forecast invalidation failed for node %s", node_id)

    logger.info("retrain_model updated %s model(s)", trained)
    return {"models": trained}


def generate_forecast(node_id: str) -> list[dict]:
    """Predict the next ``horizon_minutes`` of 1-minute-step AQI for ``node_id``.

    Plain function (not a task) so the API can call it directly. Uses a cached
    ``forecast:model:{node_id}`` if present, otherwise trains on the fly from
    the node's last-7-days readings. Predictions are clamped to ``[0, 500]``
    and cached as ``celery:forecast:{node_id}`` (TTL 3600s).

    Returns a list of ``{"time": <ISO-8601 Z, whole-second precision>, "aqi":
    <float>}`` dicts — empty when no model / data exists to forecast with.
    """
    model = _get_model(node_id)
    if model is None or "slope" not in model or "intercept" not in model:
        logger.info("No cached model for %s — training on the fly", node_id)
        model = _fit_model(_training_points(node_id))
    if not _valid_model(model):
        logger.warning("Cannot forecast node %s — no model and insufficient data", node_id)
        return []

    slope, intercept = model["slope"], model["intercept"]
    now = datetime.now(timezone.utc)
    points: list[dict] = []
    for step in range(1, FORECAST_HORIZON_MINUTES + 1):
        ts = now + timedelta(seconds=step * FORECAST_STEP_SECONDS)
        aqi = slope * ts.timestamp() + intercept
        aqi = min(500.0, max(0.0, aqi))
        # Truncate sub-second precision so point times are whole-second
        # ISO-8601 (docs/api.md), matching the documented response format.
        points.append(
            {
                "time": ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "aqi": aqi,
            }
        )

    _cache_forecast(node_id, points)
    return points
