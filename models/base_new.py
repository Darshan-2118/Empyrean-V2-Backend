"""
SQLAlchemy engine, session management, and error handling.

Provides:
- ``Base`` — declarative base with a standard naming convention
- ``AsyncEngine`` / ``SyncEngine`` — pre-configured engines (read from app config)
- ``AsyncSessionLocal`` / ``SyncSessionLocal`` — session factories
- ``get_sync_db()`` — sync context manager for Celery tasks
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import get_config

logger = logging.getLogger("empyrean.db")

# ── Naming convention for constraints & indexes ──────────────────────────────
NAMING_CONVENTION = {
    "ix": "%(column_0_label)s_idx",
    "uq": "%(table_name)s_%(column_0_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}

_metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Declarative base with shared naming convention."""
    metadata = _metadata


# ── Lazy-initialized engines ──────────────────────────────────────────────────

# Module-level state for lazy initialization
_state = {
    "sync_engine": None,
    "async_engine": None,
    "async_session_local": None,
    "sync_session_local": None,
}


def _init_engines():
    """Initialize or reinitialize database engines and session factories.
    
    Defers engine creation until the application factory runs, ensuring
    config is properly initialized. This fixes test isolation issues where
    config resets could leave stale engine instances with old URLs.
    """
    if _state["sync_engine"] is not None:
        logger.debug("Disposing stale database engines (config reset detected)")
        _state["sync_engine"].dispose()
    
    cfg = get_config()
    sync_db_url = cfg.DATABASE_URL
    
    # Build an async-compatible URL by swapping the driver
    async_db_url = make_url(sync_db_url).set(drivername="postgresql+asyncpg")

    _state["sync_engine"] = create_engine(
        sync_db_url,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        pool_pre_ping=True,
        echo=False,
    )

    _state["async_engine"] = create_async_engine(
        async_db_url,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        pool_pre_ping=True,
        echo=False,
        connect_args={"prepared_statement_cache_size": 0},
    )

    _state["async_session_local"] = async_sessionmaker(
        bind=_state["async_engine"],
        class_=AsyncSession,
        expire_on_commit=False,
    )

    _state["sync_session_local"] = sessionmaker(
        bind=_state["sync_engine"],
        expire_on_commit=False,
    )
    
    logger.debug("Database engines initialized with URL: %s", sync_db_url)


def get_sync_engine():
    """Get sync engine, initializing if needed."""
    if _state["sync_engine"] is None:
        _init_engines()
    return _state["sync_engine"]


def get_async_engine():
    """Get async engine, initializing if needed."""
    if _state["async_engine"] is None:
        _init_engines()
    return _state["async_engine"]


def get_async_session_local():
    """Get async session factory, initializing if needed."""
    if _state["async_session_local"] is None:
        _init_engines()
    return _state["async_session_local"]


def get_sync_session_local():
    """Get sync session factory, initializing if needed."""
    if _state["sync_session_local"] is None:
        _init_engines()
    return _state["sync_session_local"]


# Initialize engines on first import for backward compatibility
_init_engines()

# Export module-level variables that delegate to the getters
sync_engine = _state["sync_engine"]
async_engine = _state["async_engine"]
AsyncSessionLocal = _state["async_session_local"]
SyncSessionLocal = _state["sync_session_local"]


# ── Engine lifecycle ──────────────────────────────────────────────────────────


async def dispose_engines() -> None:
    """Dispose both engines, releasing all pooled connections.

    Call this on application shutdown to avoid leaking connections.
    """
    logger.info("Disposing database engines …")
    if _state["sync_engine"] is not None:
        _state["sync_engine"].dispose()
    if _state["async_engine"] is not None:
        await _state["async_engine"].dispose()
    logger.info("Database engines disposed.")


# ── Session helpers ──────────────────────────────────────────────────────────


@contextmanager
def get_sync_db() -> Generator:
    """Sync context manager for Celery tasks.

    Usage::

        with get_sync_db() as session:
            user = session.query(User).first()
    """
    session = get_sync_session_local()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Database session rolled back due to error")
        raise
    finally:
        session.close()
