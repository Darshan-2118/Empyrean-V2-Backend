"""
Celery application instance for async task processing.

Broker:    Redis (configured via REDIS_URL)
Backend:   Redis (same URL)
Beat:      Scheduled tasks defined below

Task modules under ``tasks/`` are imported eagerly via ``include`` so their
``@celery_app.task``-decorated callables register with the worker.
"""

from celery import Celery

from config import get_config

cfg = get_config()

celery_app = Celery(
    "empyrean",
    broker=cfg.REDIS_URL,
    backend=cfg.REDIS_URL,
    include=["tasks.aggregation", "tasks.alerts", "tasks.forecast"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    beat_schedule={
        # ── Every 60 s ──────────────────────────────
        "alert-threshold-check": {
            "task": "tasks.alerts.check_thresholds",
            "schedule": 60.0,
        },
        # ── Every hour ──────────────────────────────
        "hourly-aggregation": {
            "task": "tasks.aggregation.hourly_aggregate",
            "schedule": 3600.0,
        },
        "forecast-model-retraining": {
            "task": "tasks.forecast.retrain_model",
            "schedule": 3600.0,
        },
        # ── Daily ───────────────────────────────────
        "data-retention-cleanup": {
            "task": "tasks.aggregation.data_retention_cleanup",
            "schedule": 86400.0,
        },
    },
)
