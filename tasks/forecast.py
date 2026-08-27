"""ML Forecast task and on-the-fly AQI prediction.

Predicts next-hour AQI (60 1-minute steps) per node via linear regression fit
on the node's last 7 days of raw readings. Hourly Celery beat task
``retrain_model`` fits models for all active nodes and caches them in Redis
under ``forecast:model:{node_id}``. On-the-fly forecasting
(``generate_forecast``) reads the model, predicts the 60 points clamped to
[0, 500], and caches the result under
``celery:forecast:{node_id}:{model-version}`` (M50 — versioned by the model's
``trained_at`` so a pre-retrain computation can never clobber fresh data).
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

# H23: a cached model older than this is treated as stale and refit from the
# DB instead of trusted. The forecast *horizon* is always derived from the
# server clock (never device input), so staleness is the only remaining way a
# poisoned Redis blob could skew predictions.
_MODEL_MAX_AGE_HOURS = 48

# Redis key TTLs.
# L26/M50: the forecast cache is keyed by model version (see
# forecast_cache_key), so the model and forecast TTLs no longer interact
# dangerously — a retrain mints a new version and stale-version entries
# simply expire on their own.
_MODEL_KEY_TTL = 3600
_FORECAST_KEY_TTL = 3600

# Legacy unversioned forecast key (pre-M50). Still deleted on retrain so
# upgraded deployments don't serve orphaned blobs from older code.
_LEGACY_FORECAST_KEY = "celery:forecast:{node_id}"


def model_cache_key(node_id: str) -> str:
    """Redis key holding the cached regression model for *node_id* (L17)."""
    return f"forecast:model:{node_id}"


def forecast_cache_key(node_id: str, version: str) -> str:
    """Redis key holding the served forecast for *node_id* at *version* (L17).

    M50: the forecast is versioned by the ``trained_at`` stamp of the model
    that produced it. A request that started *before* a retrain and finishes
    after it writes its (stale) result under the OLD version key, which no
    reader consults any more — the old race let it clobber the retrain's
    invalidation and serve stale predictions until TTL.
    """
    return f"celery:forecast:{node_id}:{version}"


def model_version(model: dict) -> str:
    """Version token for *model* — its ``trained_at`` stamp, or ``legacy``.

    Legacy blobs without ``trained_at`` (H23 still accepts them) share one
    version bucket; they predate versioning anyway. Shared with the API read
    path (api/forecast.py) so both sides derive identical keys.
    """
    return str(model.get("trained_at") or "legacy")


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


def _training_points_bulk(
    node_ids: list[str],
) -> dict[str, list[tuple[float, float]]]:
    """Load 7-day (epoch_seconds, aqi) pairs for *all* node_ids in one query.

    M49: retrain used ``_training_points`` per node — N nodes meant N+1
    sessions (one per node plus the node-list query). The bulk load is a
    single query on one session regardless of fleet size.
    """
    points: dict[str, list[tuple[float, float]]] = {nid: [] for nid in node_ids}
    if not node_ids:
        return points
    since = datetime.now(timezone.utc) - timedelta(days=_TRAIN_WINDOW_DAYS)
    stmt = (
        select(SensorReading.node_id, SensorReading.time, SensorReading.aqi)
        .where(
            SensorReading.node_id.in_(node_ids),
            SensorReading.time >= since,
            SensorReading.aqi.is_not(None),
        )
        .order_by(SensorReading.node_id.asc(), SensorReading.time.asc())
    )
    with get_sync_db() as session:
        for nid, t, aqi in session.execute(stmt):
            points[nid].append((t.timestamp(), float(aqi)))
    return points


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


def _trained_at_is_fresh(value: Any) -> bool:
    """True when ``trained_at`` parses as ISO-8601 and is within max age (H23)."""
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
    return abs(age) <= timedelta(hours=_MODEL_MAX_AGE_HOURS)


def _valid_model(data: Any) -> bool:
    """Validate that a model dictionary contains valid finite numeric slope and intercept.

    When a ``trained_at`` claim is present it must parse as ISO-8601 and be
    recent (H23); blobs without one are still accepted so legacy caches keep
    working, bounded by the finite-number checks below.
    """
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
    trained_at = data.get("trained_at")
    if trained_at is not None and not _trained_at_is_fresh(trained_at):
        return False
    return True


def _get_model(node_id: str) -> dict | None:
    """Retrieve and validate the cached model for node_id from Redis."""
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(model_cache_key(node_id))
        if not raw:
            return None
        data = json.loads(raw)
        if not _valid_model(data):
            return None
        return data
    except Exception:
        return None


def _cache_model(node_id: str, model: dict) -> None:
    """Best-effort cache of *model* so its version is discoverable (M50)."""
    r = _redis()
    if r is None:
        return
    try:
        r.setex(model_cache_key(node_id), _MODEL_KEY_TTL, json.dumps(model))
    except Exception:
        logger.warning("Failed to cache forecast model for node %s", node_id)


@celery_app.task(name="empyrean.tasks.forecast.retrain_model", **_TASK_AUTORETRY)
def retrain_model() -> dict:
    """Retrain linear regression models for all active nodes and cache in Redis.

    M49: training points for the whole fleet load in ONE query/session
    (``_training_points_bulk``) instead of one session per node.

    M50: each new model carries a fresh ``trained_at`` version; served
    forecasts are keyed by that version, so a retrain never needs to race a
    slow in-flight request — stale writes land under the old version key.
    The legacy unversioned key is still deleted for upgrade hygiene.
    """
    logger.info("Starting forecast model retraining for active nodes")
    trained_count = 0

    try:
        with get_sync_db() as session:
            stmt = select(Node.node_id).where(Node.is_active.is_(True))
            active_node_ids = list(session.scalars(stmt).all())
        all_points = _training_points_bulk(active_node_ids)
    except Exception as exc:
        logger.exception("Failed to load training data for retraining: %s", exc)
        return {"models": 0}

    r = _redis()
    for node_id in active_node_ids:
        try:
            points = all_points.get(node_id, [])
            if len(points) < _MIN_TRAIN_SAMPLES:
                continue

            model = _fit_model(points)
            if model is None:
                continue

            if r is not None:
                try:
                    r.setex(
                        model_cache_key(node_id),
                        _MODEL_KEY_TTL,
                        json.dumps(model),
                    )
                    # L-11 / M50: drop the legacy unversioned forecast blob;
                    # versioned stale entries expire on their own TTL.
                    r.delete(_LEGACY_FORECAST_KEY.format(node_id=node_id))
                except Exception as cache_err:
                    logger.warning("Failed to cache model for node %s: %s", node_id, cache_err)

            trained_count += 1
        except Exception as e:
            logger.exception("Error retraining forecast model for node %s: %s", node_id, e)

    logger.info("Forecast retraining complete. Trained %d models", trained_count)
    return {"models": trained_count}


def generate_forecast(node_id: str) -> list[dict]:
    """Generate 60 1-minute step AQI predictions for node_id.

    M50: the result is cached under a key versioned by the model's
    ``trained_at`` — a computation that started on a pre-retrain model can
    never clobber the current version's cache entry.
    """
    model = _get_model(node_id)
    if model is None:
        points = _training_points(node_id)
        model = _fit_model(points)
        if model is not None:
            # Cache the on-the-fly model so concurrent/subsequent requests
            # share it and read a consistent version (M50).
            _cache_model(node_id, model)

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
                forecast_cache_key(node_id, model_version(model)),
                _FORECAST_KEY_TTL,
                json.dumps(predictions),
            )
        except Exception:
            pass

    return predictions