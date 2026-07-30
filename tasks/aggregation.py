"""
Aggregation and data-retention Celery tasks.

Hourly roll-ups and cleanup of expired readings.
"""
from celery_app import celery_app


@celery_app.task
def hourly_aggregate() -> str:
    """Compute hourly aggregates from raw sensor_readings.

    TODO: Implement aggregation query.
    """
    return "hourly_aggregate: no-op (stub)"


@celery_app.task
def data_retention_cleanup() -> str:
    """Delete readings older than the configured retention period.

    TODO: Implement cleanup logic.
    """
    return "data_retention_cleanup: no-op (stub)"
