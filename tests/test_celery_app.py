"""Celery app configuration tests (Task 1: L-5 task limits + L-15 cron schedules).

Asserts on the *effective* values Celery exposes through ``conf`` so the test
fails if the tuning is ever reverted or misapplied.
"""

from celery.schedules import crontab

import celery_app


def test_task_time_limits_and_prefetch_tuning():
    """L-5: hung aggregation/retrain cannot hold a worker forever."""
    conf = celery_app.celery_app.conf
    assert conf.task_soft_time_limit == 300
    assert conf.task_time_limit == 600
    assert conf.worker_prefetch_multiplier == 1


def test_ack_contract_is_at_most_once_for_failures():
    """M96: failed/hard-killed tasks are acked (at-most-once for non-retriable
    failures); a worker dying mid-task still redelivers via reject_on_worker_lost."""
    conf = celery_app.celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.task_acks_on_failure_or_timeout is True


def test_task_results_are_ignored():
    """L61: nothing reads task results, so they are dropped, not stored."""
    conf = celery_app.celery_app.conf
    assert conf.task_ignore_result is True


def test_broker_startup_retries_are_bounded():
    """L63: startup retries are capped; systemd restarts after exhaustion."""
    conf = celery_app.celery_app.conf
    assert conf.broker_connection_max_retries == 30
    assert conf.broker_transport_options["max_retries"] == 30


def test_alert_check_keeps_fixed_60s_interval():
    """The monitor stays a fixed-interval check, not hour-aligned work."""
    sched = celery_app.celery_app.conf.beat_schedule
    assert sched["alert-threshold-check"]["schedule"] == 60.0


def test_hourly_aggregation_is_cron_minute_7():
    """L-15: hourly job fires 7 minutes past each hour (not minute=0)."""
    sched = celery_app.celery_app.conf.beat_schedule
    assert sched["hourly-aggregation"]["schedule"] == crontab(minute=7)


def test_model_retraining_is_cron_minute_37():
    """L62: retraining is offset from aggregation (minute 7) so the two
    heaviest DB jobs no longer collide every hour."""
    sched = celery_app.celery_app.conf.beat_schedule
    assert sched["forecast-model-retraining"]["schedule"] == crontab(minute=37)


def test_retention_cleanup_is_daily_cron_0323():
    """L-15: daily cleanup runs at 03:23 local, not raw 86400s drift."""
    sched = celery_app.celery_app.conf.beat_schedule
    assert sched["data-retention-cleanup"]["schedule"] == crontab(hour=3, minute=23)


def test_beat_schedule_has_exactly_five_keys():
    """Guard against accidental add/remove of entries (refresh-token-cleanup
    was added by M79 without updating this guard)."""
    keys = list(celery_app.celery_app.conf.beat_schedule)
    assert keys == [
        "alert-threshold-check",
        "hourly-aggregation",
        "forecast-model-retraining",
        "data-retention-cleanup",
        "refresh-token-cleanup",
    ]
