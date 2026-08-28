"""Aggregation task tests.

Pins the idempotency contract of ``hourly_aggregate`` (M46): the watermark is
re-read inclusively, so the last rolled-up hour is intentionally re-aggregated
on every run — the ``ON CONFLICT ... DO UPDATE`` UPSERT must make that a no-op
(same rows, same values, never a duplicate bucket).

The task opens its own committing ``get_sync_db()`` session, so rows are seeded
through the sync pipeline with a unique node id per run (committed rows from
other tests never collide).
"""

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from models import Node, SensorReading
from models.base import get_sync_db
from tasks.aggregation import hourly_aggregate


def _node_rows(node_id: str) -> list:
    with get_sync_db() as session:
        return session.execute(
            text(
                "SELECT bucket, reading_count, avg_pm25, max_aqi "
                "FROM hourly_agg WHERE node_id = :n ORDER BY bucket"
            ),
            {"n": node_id},
        ).all()


def test_hourly_aggregate_is_idempotent():
    """M46: a second run re-covers the watermarked hour via UPSERT — no
    duplicate bucket and identical aggregate values."""
    tag = secrets.token_hex(3)
    node_id = f"AGG-{tag.upper()}"

    # Seed into the last fully-elapsed hour. After the first run the global
    # watermark lands exactly on this bucket, so the second run is guaranteed
    # to re-cover it (start = watermark, inclusive).
    now = datetime.now(timezone.utc)
    bucket_hour = (now - timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )

    with get_sync_db() as session:
        # Deterministic watermark: no buckets from other tests may push the
        # watermark past this hour, or the re-run would skip it.
        session.execute(text("DELETE FROM hourly_agg"))
        session.add(Node(
            node_id=node_id, name="agg idempotency", location_name="Test Lab",
            lat=0.0, lon=0.0, reading_interval=30, is_active=True,
        ))
        for i in range(4):
            session.add(SensorReading(
                node_id=node_id,
                time=bucket_hour + timedelta(minutes=10 * i),
                temperature=20.0, humidity=40.0, pm25=30.0,
                aqi=90, aqi_category="Moderate", fuzzy_score=40.0,
                is_anomaly=False,
            ))

    first = hourly_aggregate()
    assert first["buckets"] >= 1

    rows1 = _node_rows(node_id)
    assert len(rows1) == 1, f"expected exactly one bucket, got {rows1!r}"
    assert rows1[0][1] == 4  # reading_count

    second = hourly_aggregate()  # re-aggregates the watermarked hour
    rows2 = _node_rows(node_id)

    # Idempotent: still exactly one bucket, identical values.
    assert rows2 == rows1
    assert second["buckets"] >= 0


def test_hourly_aggregate_folds_late_readings_behind_watermark():
    """L71: a late reading landing in an already-closed hour behind the
    watermark (H25 accepts device timestamps up to 24h in the past) is folded
    in on the next run instead of being skipped forever."""
    tag = secrets.token_hex(3)
    node_id = f"AGG-{tag.upper()}"

    now = datetime.now(timezone.utc)
    closed_hour = (now - timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )
    late_hour = closed_hour - timedelta(hours=1)

    with get_sync_db() as session:
        session.execute(text("DELETE FROM hourly_agg"))
        session.add(Node(
            node_id=node_id, name="agg late fold", location_name="Test Lab",
            lat=0.0, lon=0.0, reading_interval=30, is_active=True,
        ))
        session.add(SensorReading(
            node_id=node_id,
            time=closed_hour + timedelta(minutes=5),
            temperature=20.0, humidity=40.0, pm25=30.0,
            aqi=90, aqi_category="Moderate", fuzzy_score=40.0,
            is_anomaly=False,
        ))

    first = hourly_aggregate()
    assert first["buckets"] >= 1
    assert len(_node_rows(node_id)) == 1

    with get_sync_db() as session:
        # Late reading for an hour already BEHIND the watermark (within 24h).
        session.add(SensorReading(
            node_id=node_id,
            time=late_hour + timedelta(minutes=5),
            temperature=25.0, humidity=50.0, pm25=35.0,
            aqi=100, aqi_category="Moderate", fuzzy_score=45.0,
            is_anomaly=False,
        ))

    hourly_aggregate()
    rows = _node_rows(node_id)
    assert len(rows) == 2, f"expected the late hour folded in, got {rows!r}"
    late_rows = [r for r in rows if r[0] == late_hour]
    assert late_rows and late_rows[0][1] == 1  # reading_count
