"""
Alert-related Celery tasks.

Check AQI thresholds and create alert records when breaches are detected.
"""
from celery_app import celery_app


@celery_app.task
def check_thresholds() -> str:
    """Periodic task: evaluate latest readings against configured thresholds.

    TODO: Implement threshold evaluation logic.
    """
    return "check_thresholds: no-op (stub)"
