"""
pytest configuration — fixtures for the entire test suite.

Uses a separate PostgreSQL database (``empyrean_test``) so tests never touch
real data.  Tables are created once per session; each test function runs in
its own transaction that gets rolled back on exit.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from config import get_config, reset_config_cache

# ── Test database ─────────────────────────────────────────────────────────────
cfg = get_config()

# Derive test DB URL by swapping the DB name, preserving any ?query params.
# Honor an explicit TEST_DATABASE_URL override.
_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or (
    make_url(cfg.DATABASE_URL).set(database="empyrean_test").render_as_string(hide_password=False)
)


def _ensure_test_db_exists(url_str: str) -> None:
    """Create the test DB if it doesn't already exist (no-op otherwise)."""
    url = make_url(url_str)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        admin.dispose()


_ensure_test_db_exists(_TEST_DB_URL)

# Point the application's engines at the test DB *before* the models import
# below (models/base.py builds its engines from DATABASE_URL at import time).
# Without this, API-level tests would silently hit the real "Empyrean" DB.
os.environ["DATABASE_URL"] = _TEST_DB_URL

# N-8: get_config() caches its first Config. We built one above (from .env,
# whose DATABASE_URL points at the real "Empyrean" DB) to derive the test URL;
# drop that cached instance so the models import picks up the env override.
reset_config_cache()

from models import Base, Node, SystemSetting, User
from models.helpers import hash_password

_engine = create_engine(_TEST_DB_URL, pool_pre_ping=True)
_SessionFactory = sessionmaker(bind=_engine)


# ── Session-scoped: create / drop tables ──────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def create_test_tables():
    """Create all tables once per test session, then drop them and dispose."""
    Base.metadata.create_all(_engine)
    # N-1: ensure no async-pool connection can serve a catalog that predates
    # create_all, so a later async endpoint test never hits a stale
    # "relation does not exist". Dispose the async engine's pool upfront; at
    # session start there should be no live connections, but wrap defensively
    # so an event-loop mismatch can never break the fixture.
    import asyncio
    from models.base import async_engine

    try:
        asyncio.run(async_engine.dispose())
    except Exception:  # noqa: BLE001 - defensive: pool state must not break teardown
        pass
    yield
    Base.metadata.drop_all(_engine)
    _engine.dispose()


# ── Function-scoped: isolated transaction ─────────────────────────────────────

@pytest.fixture
def db_session() -> Session:
    """Yield a session inside a transaction, rolling back after each test."""
    connection = _engine.connect()
    transaction = connection.begin()
    session = _SessionFactory(bind=connection)

    yield session

    session.close()
    # A failed flush (e.g. the IntegrityError tests) aborts the session's
    # transaction; closing the session detaches it from the connection, so
    # only roll back while the underlying transaction is still active.
    if transaction.is_active:
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
