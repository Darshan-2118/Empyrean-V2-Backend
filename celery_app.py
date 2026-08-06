"""
Celery application instance for async task processing.

Broker:    Redis (configured via REDIS_URL)
Backend:   Redis (same URL)
Beat:      Scheduled tasks defined below

Task modules under ``tasks/`` are imported eagerly via ``include`` so their
``@celery_app.task``-decorated callables register with the worker.
"""

from celery import Celery
from celery.schedules import crontab

from config import get_config

cfg = get_config()

celery_app = Celery(
    "empyrean",
    broker=cfg.REDIS_URL,
    backend=cfg.REDIS_URL,
    include=[
        "tasks.aggregation",
        "tasks.alerts",
        "tasks.forecast",
        "tasks.process_reading",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    # At-least-once delivery (M-6): ack only after the task finishes, reject
    # instead of silently acking when a worker is lost, and never auto-ack a
    # failed/timeout task. This makes per-message redelivery possible, so the
    # PK-duplicate guard in tasks/process_reading.py actually fires on replay.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_acks_on_failure_or_timeout=False,
    # L-5: finite time bounds so a hung aggregation/retrain cannot hold a worker
    # + pool connection indefinitely; prefetch=1 stops workers from hoarding a
    # pile of long tasks ahead of completion.
    task_soft_time_limit=300,
    task_time_limit=600,
    worker_prefetch_multiplier=1,
    beat_schedule={
        # ── Every 60 s ──────────────────────────────
        "alert-threshold-check": {
            "task": "tasks.alerts.check_thresholds",
            "schedule": 60.0,
        },
        # ── Every hour (7 minutes past, to dodge the top-of-hour stampede) ──
        "hourly-aggregation": {
            "task": "tasks.aggregation.hourly_aggregate",
            "schedule": crontab(minute=7),
        },
        "forecast-model-retraining": {
            "task": "tasks.forecast.retrain_model",
            "schedule": crontab(minute=7),
        },
        # ── Daily at 03:23 ──────────────────────────
        "data-retention-cleanup": {
            "task": "tasks.aggregation.data_retention_cleanup",
            "schedule": crontab(hour=3, minute=23),
        },
    },
)
