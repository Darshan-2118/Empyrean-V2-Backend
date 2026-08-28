"""Data-layer tests: DB-side server_defaults (M-17) and FK cascade behavior.

These close the biggest data-layer coverage gaps (L-39) and pin M-17: the 8
NOT NULL columns that used to carry only a Python-side ORM default must now
declare a ``server_default`` so raw/bulk SQL inserts work.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from models import Alert, Base, HourlyAgg, Node, RefreshToken, User

# The 8 columns M-17 targeted — each must now carry a DB-side server_default.
TARGETED_SERVER_DEFAULTS = {
    (User.__tablename__, "role"),
    (User.__tablename__, "notification_prefs"),
    (User.__tablename__, "is_active"),
    (RefreshToken.__tablename__, "revoked"),
    (Node.__tablename__, "reading_interval"),
    (Node.__tablename__, "is_active"),
    (HourlyAgg.__tablename__, "anomaly_count"),
    (HourlyAgg.__tablename__, "reading_count"),
}


def test_get_sync_db_chains_rollback_error_to_original(monkeypatch):
    """L68: when the rollback itself fails, the raised error chains back to
    the original failure instead of suppressing it (old ``from None``)."""
    import models.base as base

    class _FakeSession:
        def commit(self):
            raise ValueError("original failure")

        def rollback(self):
            raise RuntimeError("rollback failed")

        def close(self):
            pass

    monkeypatch.setattr(base, "get_sync_session_local", lambda: (lambda: _FakeSession()))
    with pytest.raises(RuntimeError) as excinfo:
        with base.get_sync_db():
            pass
    assert isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.parametrize("tbl,col", sorted(TARGETED_SERVER_DEFAULTS))
def test_server_default_declared(tbl, col):
    """Each M-17 column declares a ``server_default`` on its mapped column."""
    table = Base.metadata.tables[tbl]
    assert table.columns[col].server_default is not None, f"{tbl}.{col}"


def test_raw_insert_gets_server_defaults(db_session):
    """Raw/bulk SQL inserts skipping these columns get the DB defaults filled."""
    db_session.execute(text(
        "INSERT INTO users (username, email, password_hash) "
        "VALUES ('raw_admin', 'raw@test.local', 'x')"
    ))
    db_session.execute(text(
        "INSERT INTO refresh_tokens (user_id, token_hash, expires_at) "
        "SELECT id, 'hash', now() + interval '1 hour' FROM users "
        "WHERE username = 'raw_admin'"
    ))
    db_session.execute(text(
        "INSERT INTO nodes (node_id, name) VALUES ('RAW-NODE', 'Raw Node')"
    ))
    db_session.execute(text(
        "INSERT INTO hourly_agg (bucket, node_id) VALUES (now(), 'RAW-NODE')"
    ))

    # No commit needed: server_defaults apply at INSERT time inside the
    # transaction, and the db_session fixture rolls back on teardown.
    user = db_session.execute(text(
        "SELECT role, notification_prefs, is_active FROM users "
        "WHERE username = 'raw_admin'"
    )).one()
    assert user.role == "user"
    assert user.notification_prefs == {}
    assert user.is_active is True

    revoked = db_session.execute(text(
        "SELECT revoked FROM refresh_tokens WHERE token_hash = 'hash'"
    )).scalar()
    assert revoked is False

    node = db_session.execute(text(
        "SELECT reading_interval, is_active FROM nodes WHERE node_id = 'RAW-NODE'"
    )).one()
    assert node.reading_interval == 30
    assert node.is_active is True

    agg = db_session.execute(text(
        "SELECT anomaly_count, reading_count FROM hourly_agg "
        "WHERE node_id = 'RAW-NODE'"
    )).one()
    assert agg.anomaly_count == 0
    assert agg.reading_count == 0


def test_node_delete_cascades_to_reading(db_session, sample_node):
    """ON DELETE CASCADE removes a node's readings at the DB level."""
    db_session.execute(text(
        "INSERT INTO sensor_readings (time, node_id, pm25) "
        "VALUES (now(), 'TEST-ESP32-01', 12.5)"
    ))
    assert db_session.execute(text(
        "SELECT COUNT(*) FROM sensor_readings WHERE node_id = 'TEST-ESP32-01'"
    )).scalar() == 1

    db_session.execute(text("DELETE FROM nodes WHERE node_id = 'TEST-ESP32-01'"))
    assert db_session.execute(text(
        "SELECT COUNT(*) FROM sensor_readings WHERE node_id = 'TEST-ESP32-01'"
    )).scalar() == 0


def test_node_delete_cascades_to_alerts(db_session, sample_node, admin_user):
    """ON DELETE CASCADE also removes a node's alerts at the DB level."""
    db_session.add(Alert(
        node_id="TEST-ESP32-01", parameter="aqi",
        value=160.0, threshold=150.0, severity="warning",
    ))
    db_session.flush()
    assert db_session.execute(text(
        "SELECT COUNT(*) FROM alerts WHERE node_id = 'TEST-ESP32-01'"
    )).scalar() == 1

    db_session.execute(text("DELETE FROM nodes WHERE node_id = 'TEST-ESP32-01'"))
    assert db_session.execute(text(
        "SELECT COUNT(*) FROM alerts WHERE node_id = 'TEST-ESP32-01'"
    )).scalar() == 0


def test_alerts_partial_unique_blocks_second_unacked(db_session, sample_node):
    """The M-4 partial unique index allows at most one *unacknowledged* alert
    per (node_id, parameter) — the DB, not app logic, arbitrates dedupe."""
    db_session.add(Alert(
        node_id="TEST-ESP32-01", parameter="aqi",
        value=160.0, threshold=150.0, severity="warning",
    ))
    db_session.flush()

    with pytest.raises(IntegrityError):
        # Same node + parameter, still unacknowledged → must violate
        # uq_alerts_unacked_node.
        db_session.add(Alert(
            node_id="TEST-ESP32-01", parameter="aqi",
            value=210.0, threshold=200.0, severity="critical",
        ))
        db_session.flush()


def test_alerts_partial_unique_allows_after_acknowledgement(db_session, sample_node):
    """Acknowledging an alert frees the (node_id, parameter) slot again.

    Uses its own test transaction because the previous test's IntegrityError
    aborts that transaction; each function-scoped ``db_session`` is fresh.
    """
    a1 = Alert(
        node_id="TEST-ESP32-01", parameter="aqi",
        value=160.0, threshold=150.0, severity="warning",
    )
    db_session.add(a1)
    db_session.flush()

    # Acknowledge the first alert → it drops out of the partial index window.
    a1.acknowledged_at = datetime.now(timezone.utc)
    db_session.flush()

    # Now a new unacknowledged alert for the same node + parameter is allowed.
    db_session.add(Alert(
        node_id="TEST-ESP32-01", parameter="aqi",
        value=210.0, threshold=200.0, severity="critical",
    ))
    db_session.flush()
    assert db_session.execute(text(
        "SELECT COUNT(*) FROM alerts WHERE node_id = 'TEST-ESP32-01'"
    )).scalar() == 2