"""ML Forecast task and on-the-fly AQI prediction.

Predicts next-hour AQI (60 1-minute steps) per node via linear regression fit
on the node's last 7 days of raw readings. Hourly Celery beat task
``retrain_model`` fits models for all active nodes and caches them in Redis
under ``forecast:model:{node_id}``. On-the-fly forecasting
(``generate_forecast``) reads the model, predicts the 60 points clamped to
[0, 500], and caches the result under ``celery:forecast:{node_id}``.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from celery_app import _TASK_AUTORETRY, celery_app
from models import Node, SensorReading
from models.base import get_sync_db
from tasks._redis import get_sync_redis

logger = logging.getLogger("empyrean.tasks.forecast")

FORECAST_HORIZON_MINUTES = 60
_MIN_TRAIN_SAMPLES = 30
_TRAIN_WINDOW_DAYS = 7

# Redis key TTLs
_MODEL_KEY_TTL = 3600
_FORECAST_KEY_TTL = 3600


def _redis():
    """Return the shared sync Redis client (or None)."""
    return get_sync_redis()


def _training_points(node_id: str) -> list[tuple[float, float]]:
    """Return (epoch_seconds, aqi) pairs from the last 7 days for this node."""
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
    return [(t.timestamp(), float(aqi)) for t, aqi in rows if aqi is not None]


def _fit_model(points: list[tuple[float, float]]) -> dict | None:
    """Fit a linear regression model: aqi = slope * epoch_seconds + intercept."""
    if len(points) < _MIN_TRAIN_SAMPLES:
        return None

    try:
        from sklearn.linear_model import LinearRegression
    except ImportError:
        logger.warning("scikit-learn not installed — forecasting disabled")
        return None

    xs = [[x] for x, _ in points]
    ys = [y for _, y in points]

    try:
        model = LinearRegression().fit(xs, ys)
        slope = float(model.coef_[0])
        intercept = float(model.intercept_)
        if not (math.isfinite(slope) and math.isfinite(intercept)):
            return None
        return {
            "slope": slope,
            "intercept": intercept,
            "trained_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    except Exception as exc:
        logger.warning("Failed to fit regression model: %s", exc)
        return None


def _valid_model(data: Any) -> bool:
    """Validate that a model dictionary contains valid finite numeric slope and intercept."""
    if not isinstance(data, dict):
        return False
    slope = data.get("slope")
    intercept = data.get("intercept")
    if isinstance(slope, bool) or isinstance(intercept, bool):
        return False
    if not isinstance(slope, (int, float)) or not isinstance(intercept, (int, float)):
        return False
    if not (math.isfinite(slope) and math.isfinite(intercept)):
        return False
    return True


def _get_model(node_id: str) -> dict | None:
    """Retrieve and validate the cached model for node_id from Redis."""
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(f"forecast:model:{node_id}")
        if not raw:
            return None
        data = json.loads(raw)
        if not _valid_model(data):
            return None
        return data
    except Exception:
        return None


@celery_app.task(name="tasks.forecast.retrain_model", **_TASK_AUTORETRY)
def retrain_model() -> dict:
    """Retrain linear regression models for all active nodes and cache in Redis."""
    logger.info("Starting forecast model retraining for active nodes")
    trained_count = 0

    try:
        with get_sync_db() as session:
            stmt = select(Node.node_id).where(Node.is_active.is_(True))
            active_node_ids = list(session.scalars(stmt).all())
    except Exception as exc:
        logger.exception("Failed to query active nodes for retraining: %s", exc)
        return {"models": 0}

    r = _redis()
    for node_id in active_node_ids:
        try:
            points = _training_points(node_id)
            if len(points) < _MIN_TRAIN_SAMPLES:
                continue

            model = _fit_model(points)
            if model is None:
                continue

            if r is not None:
                try:
                    r.setex(
                        f"forecast:model:{node_id}",
                        _MODEL_KEY_TTL,
                        json.dumps(model),
                    )
                    # L-11: invalidate served forecast cache so stale predictions are dropped
                    r.delete(f"celery:forecast:{node_id}")
                except Exception as cache_err:
                    logger.warning("Failed to cache model for node %s: %s", node_id, cache_err)

            trained_count += 1
        except Exception as e:
            logger.exception("Error retraining forecast model for node %s: %s", node_id, e)

    logger.info("Forecast retraining complete. Trained %d models", trained_count)
    return {"models": trained_count}


def generate_forecast(node_id: str) -> list[dict]:
    """Generate 60 1-minute step AQI predictions for node_id."""
    model = _get_model(node_id)
    if model is None:
        points = _training_points(node_id)
        model = _fit_model(points)

    if model is None:
        return []

    now = datetime.now(timezone.utc)
    predictions: list[dict[str, Any]] = []

    for step in range(1, FORECAST_HORIZON_MINUTES + 1):
        target_time = (now + timedelta(minutes=step)).replace(microsecond=0)
        ts = target_time.timestamp()
        raw_aqi = model["slope"] * ts + model["intercept"]
        clamped_aqi = max(0.0, min(500.0, raw_aqi))
        predictions.append({
            "time": target_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "aqi": round(clamped_aqi, 1),
        })

    r = _redis()
    if r is not None:
        try:
            r.setex(
                f"celery:forecast:{node_id}",
                _FORECAST_KEY_TTL,
                json.dumps(predictions),
            )
        except Exception:
            pass

    return predictions