"""
Forecast-related Celery tasks.

Model retraining and next-hour AQI predictions.
"""
from celery_app import celery_app


@celery_app.task
def retrain_model() -> str:
    """Retrain the forecast model with recent sensor data.

    TODO: Implement model retraining logic.
    """
    return "retrain_model: no-op (stub)"
