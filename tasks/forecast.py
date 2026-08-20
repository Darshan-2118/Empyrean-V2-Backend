from __future__ import annotations
import logging
import math
from datetime import datetime, timedelta, timezone

from celery_app import _TASK_AUTORETRY, celery_app
from models import Node, SensorReading
from models.base import get_sync_db
from tasks._redis import get_sync_redis

logger = logging.getLogger("empyrean.tasks.forecast")

# Forecast parameters
FORECAST_HORIZON_MINUTES = 60
FORECAST_STEP_SECONDS = 60
_MIN_TRAIN_SAMPLES = 30
_TRAIN_WINDOW_DAYS = 7

# --- Added seasonal adjustment to Issue #37 ---
_SEASONAL_WINDOW_MONTHS = 12  # Consider 12 months for seasonal patterns

# Redis key TTLs
_MODEL_KEY_TTL = 3600
_FORECAST_KEY_TTL = 3600

# --- Data retrieval / model helpers ---

def _training_points(node_id: str) -> list[tuple[float, float, int]]:  # Added month as feature
    """Return (epoch_seconds, aqi, month) triples for seasonal adjustment."""
    since = datetime.now(timezone.utc) - timedelta(days=_TRAIN_WINDOW_DAYS)
    stmt = (
        select(SensorReading.time, SensorReading.aqi)
        .where(
            SensorReading.node_id == node_id,
            SensorReading.time >= since,
            SensorReading.aqi.is_not(None)
        )
        .order_by(SensorReading.time.asc())
    )
    with get_sync_db() as session:
        rows = session.execute(stmt).all()
    return [
        (t.timestamp(), float(aqi), t.month())  # Extract month for seasonality
        for t, aqi in rows
    ]


def _fit_model(points: list[tuple[float, float, int]]) -> dict | None:
    """Fit multiple linear regression with slope, intercept, and seasonal coefficients."""
    if len(points) < _MIN_TRAIN_SAMPLES:
        return None

    try:
        from sklearn.linear_model import LinearRegression
    except ImportError:
        logger.warning("scikit-learn not installed")
        return None

    model = LinearRegression()
    xs = [[x, m] for x, _, m in points]  # Add month as feature
    ys = [y for _, y, _ in points]
    model.fit(xs, ys)
    return {
        "slope": float(model.coef_[0]),
        "intercept": float(model.intercept_),
        "seasonal_coeffs": {m: coef for m, coef in zip(*model.coef_[1:])},
        "trained_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }

# --- Redis and forecasting logic remains unchanged ---