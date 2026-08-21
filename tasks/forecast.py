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


@celery_app.task(name="tasks.forecast.retrain_model", **_TASK_AUTORETRY)
def retrain_model() -> dict:
    """Retrain forecast models for all active nodes and store in Redis."""
    logger.info("Starting forecast model retraining for all active nodes")
    redis_client = get_sync_redis()
    if redis_client is None:
        logger.warning("Redis client not available, skipping model retraining")
        return {"trained": 0, "skipped": "no_redis"}

    try:
        # Get all active nodes
        with get_sync_db() as session:
            active_nodes = session.scalars(select(Node).where(Node.is_active.is_(True))).all()
            node_ids = [node.node_id for node in active_nodes]
        logger.info("Found %d active nodes for forecast retraining", len(node_ids))

        trained_count = 0
        for node_id in node_ids:
            try:
                # Get training points for this node
                points = _training_points(node_id)
                if len(points) < _MIN_TRAIN_SAMPLES:
                    logger.debug("Insufficient training points for node %s (%d < %d)",
                                node_id, len(points), _MIN_TRAIN_SAMPLES)
                    continue

                # Fit model
                model_dict = _fit_model(points)
                if model_dict is None:
                    logger.warning("Model fitting failed for node %s", node_id)
                    continue

                # Store model in Redis
                model_key = f"forecast_model:{node_id}"
                import json
                model_json = json.dumps(model_dict)
                redis_client.set(model_key, model_json, ex=_MODEL_KEY_TTL)
                logger.debug("Stored forecast model for node %s in Redis with key %s",
                            node_id, model_key)
                trained_count += 1

            except Exception as e:
                logger.exception("Error retraining forecast model for node %s: %s", node_id, e)
                continue

        logger.info("Forecast model retraining completed. Trained models: %d/%d",
                   trained_count, len(node_ids))
        return {"trained": trained_count, "total_nodes": len(node_ids)}

    except Exception as e:
        logger.exception("Error during forecast model retraining: %s", e)
        return {"trained": 0, "error": str(e)}
    finally:
        try:
            redis_client.close()
        except Exception:
            pass


def generate_forecast(node_id: str, hours_ahead: int = 1) -> dict:
    """Generate AQI forecasts for the next {hours_ahead} hours for a node.

    Uses the trained model stored in Redis to predict future AQI values.
    Returns an empty dict if no model is available or forecast generation fails.

    Args:
        node_id: Node identifier for which to generate forecast
        hours_ahead: Number of hours ahead to forecast (default: 1)

    Returns:
        Dictionary with forecast results or error info
    """
    import json

    logger.info("Generating forecast for node %s, %d hours ahead", node_id, hours_ahead)

    # Retrieve trained model from Redis
    model_key = f"forecast_model:{node_id}"
    with get_sync_redis() as redis_client:
        model_json = redis_client.get(model_key)
        if not model_json:
            logger.warning("No forecast model found for node %s", node_id)
            return {"prediction_hours": hours_ahead, "available": False, "error": "no_model"}

        model = json.loads(model_json)

    # Generate forecast using the trained model
    try:
        forecast_data = []
        current_time = datetime.now(timezone.utc).timestamp()

        for step in range(1, hours_ahead + 1):
            forecast_time = current_time + (step * FORECAST_STEP_SECONDS)
            month = (datetime.fromtimestamp(forecast_time, tz=timezone.utc)).month

            # Linear regression: AQI = slope * x + intercept + seasonal_coeffs[month]
            forecast_aqi = model["slope"] * forecast_time + model["intercept"]

            # Add seasonal adjustment if available
            if month in model["seasonal_coeffs"]:
                forecast_aqi += model["seasonal_coeffs"][month]

            forecast_data.append({
                "hour_step": step,
                "timestamp": datetime.fromtimestamp(forecast_time, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "aqi": round(forecast_aqi, 2),
                "month": month
            })

        logger.info("Generated forecast for node %s: %d hours", node_id, hours_ahead)
        return {
            "prediction_hours": hours_ahead,
            "available": True,
            "trained_at": model["trained_at"],
            "forecasts": forecast_data
        }

    except Exception as e:
        logger.exception("Error generating forecast for node %s: %s", node_id, e)
        return {
            "prediction_hours": hours_ahead,
            "available": False,
            "error": str(e)
        }