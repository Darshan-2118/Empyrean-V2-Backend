"""
SQLAlchemy engine, session management, and error handling.

Provides:
- ``Base`` — declarative base with a standard naming convention
- ``AsyncEngine`` / ``SyncEngine`` — pre-configured engines (read from app config)
- ``AsyncSessionLocal`` / ``SyncSessionLocal`` — session factories
- ``get_sync_db()`` — sync context manager for Celery tasks
"""

import asyncio
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
    """Declarative base with shared naming convention.

    **Timezone Assumption**: All timestamp fields in this model use PostgreSQL's
    TIMESTAMP with timezone. Data should be stored in UTC. When setting timestamps
    directly (e.g., datetime.now(timezone.utc)), the value is automatically
    converted to/from UTC by PostgreSQL's timezone-aware column type.

    Example:
        # ✅ GOOD - UTC timestamp (recommended)
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc)

        # ❌ BAD - Local timezone (not recommended)
        timestamp = datetime.now()  # Uses local system timezone

    See: docs/timezone.md for detailed timezone handling conventions.
    """
    metadata = _metadata


# ── Lazy-initialized engines ──────────────────────────────────────────────────

# Module-level state for lazy initialization
_state = {
    "sync_engine": None,
    "async_engine": None,
    "async_session_local": None,
    "sync_session_local": None,
}


def _dispose_stale_async_engine() -> None:
    """Best-effort dispose of a leftover async engine before replacing it.

    M89: reinit used to dispose only the sync engine, leaking the async
    pool. ``AsyncEngine.dispose()`` is a coroutine and reinit is sync, so
    run it in a fresh loop when possible; inside a running loop (or if the
    pool was bound to a dead one) fall back to dropping the reference.
    """
    stale = _state["async_engine"]
    if stale is None:
        return
    try:
        asyncio.run(stale.dispose())
    except Exception:
        logger.debug("Could not dispose stale async engine pool", exc_info=True)


def _init_engines():
    """Initialize or reinitialize database engines and session factories.
    
    Defers engine creation until the application factory runs, ensuring
    config is properly initialized. This fixes test isolation issues where
    config resets could leave stale engine instances with old URLs.
    """
    if _state["sync_engine"] is not None:
        logger.debug("Disposing stale database engines (config reset detected)")
        _state["sync_engine"].dispose()
    _dispose_stale_async_engine()

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
        # L30: prepared_statement_cache_size=0 disables asyncpg's prepared-
        # statement cache on purpose — with pgbouncer in transaction-pooling
        # mode a statement prepared on one connection can be executed on
        # another that never prepared it ("prepared statement does not
        # exist"). Keep 0 while pgbouncer sits in front of Postgres.
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
    
    # L58: never log the raw URL — it embeds the DB password and DEBUG is an
    # operator-allowed log level.
    logger.debug(
        "Database engines initialized with URL: %s",
        make_url(sync_db_url).render_as_string(hide_password=True),
    )


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


# M58: ``sync_engine`` / ``async_engine`` / ``AsyncSessionLocal`` /
# ``SyncSessionLocal`` are resolved lazily via PEP 562 ``__getattr__`` instead
# of being bound once at import time. A reinit or reset_engines() can then
# never leave ``models.base.<alias>`` lookups pointing at disposed engines —
# each access returns whatever the current state holds (initializing on first
# access). Note: a ``from models.base import …`` still captures the object at
# import time; after a reset, re-import or use the getters for a fresh one.


def __getattr__(name: str):
    if name == "sync_engine":
        return get_sync_engine()
    if name == "async_engine":
        return get_async_engine()
    if name == "AsyncSessionLocal":
        return get_async_session_local()
    if name == "SyncSessionLocal":
        return get_sync_session_local()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── Engine lifecycle ──────────────────────────────────────────────────────────


def reset_engines() -> None:
    """Dispose and drop all engines/session factories (M57).

    Wired into ``config.reset_config_cache()``: engines are built from
    ``DATABASE_URL``, so after a config reset the next engine access must
    rebuild from the fresh URL instead of reusing engines built from the old
    one. Safe to call when nothing was built yet.
    """
    if _state["sync_engine"] is not None:
        _state["sync_engine"].dispose()
    _dispose_stale_async_engine()
    _state["sync_engine"] = None
    _state["async_engine"] = None
    _state["async_session_local"] = None
    _state["sync_session_local"] = None


async def dispose_engines() -> None:
    """Dispose both engines, releasing all pooled connections.

    Call this on application shutdown to avoid leaking connections.

    M89: also clears the module state — otherwise post-shutdown getters would
    hand back the disposed engines instead of re-initializing fresh ones.
    """
    logger.info("Disposing database engines …")
    if _state["sync_engine"] is not None:
        _state["sync_engine"].dispose()
    if _state["async_engine"] is not None:
        await _state["async_engine"].dispose()
    _state["sync_engine"] = None
    _state["async_engine"] = None
    _state["async_session_local"] = None
    _state["sync_session_local"] = None
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
    except Exception as original_exc:
        # Log the original exception before rollback to preserve debuggability
        logger.exception("Database session rolled back due to error")
        try:
            session.rollback()
        except Exception as rollback_error:
            # L68: chain the rollback error to the original failure so the
            # exception that caused the rollback stays visible (``from None``
            # suppressed it and broke upstream IntegrityError handling).
            logger.error("Database rollback also failed: %s", rollback_error, exc_info=True)
            raise rollback_error from original_exc
        raise
    finally:
        session.close()
