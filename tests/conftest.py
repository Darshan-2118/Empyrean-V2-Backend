"""
pytest configuration — fixtures for the entire test suite.

Uses a separate PostgreSQL database (``Empyrean_test``) so tests never touch
real data.  Tables are created once per session; each test function runs in
its own transaction that gets rolled back on exit.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from config import get_config
from models import Base, Node, SystemSetting, User
from models.helpers import hash_password

# ── Test database ─────────────────────────────────────────────────────────────
cfg = get_config()
_TEST_DB_URL = cfg.DATABASE_URL.replace("Empyrean", "empyrean_test")

_engine = create_engine(_TEST_DB_URL, pool_pre_ping=True)
_SessionFactory = sessionmaker(bind=_engine)


# ── Session-scoped: create / drop tables ──────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def create_test_tables():
    """Create all tables once per test session, then drop them."""
    Base.metadata.create_all(_engine)
    yield
    Base.metadata.drop_all(_engine)


# ── Function-scoped: isolated transaction ─────────────────────────────────────

@pytest.fixture
def db_session() -> Session:
    """Yield a session inside a transaction, rolling back after each test."""
    connection = _engine.connect()
    transaction = connection.begin()
    session = _SessionFactory(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


# ── Model fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def admin_user(db_session: Session) -> User:
    """Create and return an admin user."""
    user = User(
        username="testadmin",
        email="testadmin@empyrean.local",
        password_hash=hash_password("test1234", rounds=4),
        role="admin",
        is_active=True,
        notification_prefs={"email_on_critical": True},
    )
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def regular_user(db_session: Session) -> User:
    """Create and return a regular user."""
    user = User(
        username="testuser",
        email="testuser@empyrean.local",
        password_hash=hash_password("userpass", rounds=4),
        role="user",
        is_active=True,
        notification_prefs={},
    )
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def sample_node(db_session: Session) -> Node:
    """Create and return a sample sensor node."""
    node = Node(
        node_id="TEST-ESP32-01",
        name="Test Sensor",
        location_name="Test Lab",
        lat=28.6139,
        lon=77.2090,
        firmware_version="v2.1.0",
        reading_interval=30,
        is_active=True,
    )
    db_session.add(node)
    db_session.flush()
    return node


@pytest.fixture
def default_settings(db_session: Session) -> list[SystemSetting]:
    """Create and return default system settings."""
    settings = [
        SystemSetting(key="aqi_warning_threshold", value="100",
                      description="AQI warning threshold"),
        SystemSetting(key="aqi_critical_threshold", value="150",
                      description="AQI critical threshold"),
        SystemSetting(key="alerts_enabled", value="true",
                      description="Master alert toggle"),
    ]
    for s in settings:
        db_session.add(s)
    db_session.flush()
    return settings


# ── Engine cleanup ────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def dispose_engine():
    """Dispose of the test engine after the session."""
    yield
    _engine.dispose()
