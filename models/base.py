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
    # N-1: disable SQLAlchemy's per-connection prepared-statement cache so a
    # pooled connection can never replay a statement prepared against a stale
    # (dropped/recreated) catalog, which otherwise surfaces intermittently as
    # `relation "…" does not exist` after cross-engine DDL. This arg is
    # intercepted by SQLAlchemy's asyncpg dialect (never forwarded to asyncpg).
    connect_args={"prepared_statement_cache_size": 0},
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
