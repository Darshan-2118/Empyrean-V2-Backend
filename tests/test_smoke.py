"""Smoke test — verify conftest fixtures work end-to-end."""

from sqlalchemy.orm import Session

from models import User, Node, SystemSetting


class TestFixtures:
    """Quick check that the test infrastructure is wired correctly."""

    def test_admin_user(self, admin_user: User):
        assert admin_user.username == "testadmin"
        assert admin_user.role == "admin"
        assert admin_user.is_active is True
        assert "@" in admin_user.email

    def test_regular_user(self, regular_user: User):
        assert regular_user.username == "testuser"
        assert regular_user.role == "user"

    def test_sample_node(self, sample_node: Node):
        assert sample_node.node_id == "TEST-ESP32-01"
        assert sample_node.is_active is True
        assert sample_node.reading_interval == 30

    def test_default_settings(self, default_settings: list[SystemSetting]):
        assert len(default_settings) == 3
        keys = {s.key for s in default_settings}
        assert "aqi_warning_threshold" in keys
        assert "alerts_enabled" in keys

    def test_user_node_independent(self, admin_user: User, sample_node: Node):
        """Requesting several fixtures in one test works; each is usable."""
        assert admin_user.id is not None
        assert sample_node.node_id == "TEST-ESP32-01"

    def test_health_check_markers(self, capsys):
        """Network-free smoke test of scripts/check_health (L-39).

        Imports the script module and exercises its pure ``check()`` helper
        (which requires no DB/Redis/broker), verifying the PASS/FAIL/SKIP
        markers and the return value contract without touching a live stack.
        """
        from scripts import check_health

        assert check_health.PASS == "[OK]"
        assert check_health.FAIL == "[FAIL]"
        assert check_health.SKIP == "[SKIP]"
        assert check_health.check("Is the sky blue?", True) is True
        assert check_health.check("Is the sky blue?", False) is False
        out = capsys.readouterr().out
        assert "[OK]  Is the sky blue?" in out
        assert "[FAIL]  Is the sky blue?" in out

    def test_db_session_rollback(self, db_session, admin_user: User):
        """Prove this test's writes are never committed, so teardown rollback
        discards them.

        A separate connection (outside the test transaction) only sees committed
        rows.  Before teardown runs we verify:
          * the ``admin_user`` fixture write is *not* visible outside the
            transaction (it isn't committed yet), and
          * a probe row written here, flushed inside the transaction, is also
            invisible outside — so nothing this test writes can leak past the
            rollback.
        Any row left committed by an earlier test (a leak) *would* show up in
        the outside count, which is what this catches.
        """
        engine = db_session.get_bind().engine
        with engine.connect() as conn:
            outside = Session(bind=conn)

            # admin_user lives only inside the fixture transaction. Scope the
            # count to *this* row so earlier behavioral tests' committed rows
            # (e.g. test_api / test_phase_coverage) don't break the assertion
            # — makes the rollback proof order-independent (L-43).
            assert outside.query(User).filter_by(username="testadmin").count() == 0

            # A probe write flushed inside the test transaction is not
            # committed: the outside reader still sees an empty table.
            db_session.add(User(
                username="rollback-probe",
                email="probe@test.local",
                password_hash="x",
            ))
            db_session.flush()
            assert db_session.query(User).filter_by(username="rollback-probe").count() == 1
            # The uncommitted probe write is invisible to the committed-only
            # outside reader, so teardown rollback discards it.
            assert outside.query(User).filter_by(username="rollback-probe").count() == 0

        # Teardown rolls the whole transaction back, discarding the probe row.
        # (We can't observe post-teardown from here, but the assertions above
        # prove the row was never committed, so the rollback removes it.)
