"""
SQLAlchemy engine, session management, and error handling.

Provides:
- ``Base`` — declarative base with a standard naming convention
- ``AsyncEngine`` / ``SyncEngine`` — pre-configured engines (read from app config)
- ``AsyncSessionLocal`` / ``SyncSessionLocal`` — session factories
- ``async_db_session()`` — async context manager that commits/rolls back sessions
- ``get_db()`` — async generator for FastAPI/Quart dependency injection
- ``get_sync_db()`` — sync context manager for Celery tasks
- Custom exceptions: ``DatabaseError``, ``NotFoundError``, ``DuplicateError``
"""

import logging
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncGenerator, Generator

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


# ── Custom exceptions ────────────────────────────────────────────────────────


class DatabaseError(Exception):
    """Base exception for database-related errors."""


class NotFoundError(DatabaseError):
    """Raised when a requested resource does not exist."""


class DuplicateError(DatabaseError):
    """Raised when a unique-constraint violation occurs."""


# ── Engines ───────────────────────────────────────────────────────────────────

_cfg = get_config()
_sync_db_url = _cfg.DATABASE_URL

# Build an async-compatible URL by swapping the driver, so any
# postgresql:// (or postgresql+<driver>://) form becomes asyncpg.
_async_db_url = make_url(_sync_db_url).set(drivername="postgresql+asyncpg")

sync_engine = create_engine(
    _sync_db_url,
    pool_size=5,
    max_overflow=10,
    pool_recycle=1800,
    pool_pre_ping=True,
    echo=False,
)

async_engine = create_async_engine(
    _async_db_url,
    pool_size=5,
    max_overflow=10,
    pool_recycle=1800,
    pool_pre_ping=True,
    echo=False,
)

# ── Session factories ─────────────────────────────────────────────────────────

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    expire_on_commit=False,
)


# ── Engine lifecycle ────────────────────────────────────────────────────────────


async def dispose_engines() -> None:
    """Dispose both engines, releasing all pooled connections.

    Call this on application shutdown (e.g. in a ``after_serving`` hook)
    to avoid leaking connections.
    """
    logger.info("Disposing database engines …")
    sync_engine.dispose()
    await async_engine.dispose()
    logger.info("Database engines disposed.")


# ── Session helpers ───────────────────────────────────────────────────────────


@asynccontextmanager
async def async_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that yields a committed (or rolled-back) session.

    Commits on success, rolls back and re-raises on error, and always closes
    the session.  Usage::

        async with async_db_session() as session:
            user = await session.get(User, 1)
    """
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Database session rolled back due to error")
        raise
    finally:
        await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI/Quart-compatible async session dependency.

    Thin wrapper over :func:`async_db_session` for dependency-injection
    style usage::

        async def route(session: AsyncSession = Depends(get_db)) -> ...:
            ...
    """
    async with async_db_session() as session:
        yield session


@contextmanager
def get_sync_db() -> Generator:
    """Sync context manager for Celery tasks.

    Usage::

        with get_sync_db() as session:
            user = session.query(User).first()
    """
    session = SyncSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Database session rolled back due to error")
        raise
    finally:
        session.close()
