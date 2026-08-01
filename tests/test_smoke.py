"""Smoke test — verify conftest fixtures work end-to-end."""

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

    def test_db_session_rollback(self, db_session, admin_user: User):
        """Each test starts from a clean users table.

        The count is taken inside the same transaction that created
        ``admin_user``, so it can't prove this test's own writes are rolled
        back (that happens after the test returns).  It does catch rows left
        over by earlier tests, which would show up here as extra users.
        """
        count = db_session.query(User).count()
        assert count == 1  # only the admin_user from this test's fixture
