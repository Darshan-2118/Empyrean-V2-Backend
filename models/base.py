"""
SQLAlchemy engine, session management, and error handling.

Provides:
- ``Base`` — declarative base with a standard naming convention
- ``AsyncEngine`` / ``SyncEngine`` — pre-configured engines (read from app config)
- ``AsyncSessionLocal`` / ``SyncSessionLocal`` — session factories
- ``get_db()`` — async generator for FastAPI/Quart dependency injection
- ``get_sync_db()`` — sync context manager for Celery tasks
- Retry logic for transient database failures
"""

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import AsyncGenerator, Generator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DatabaseError as SAError, IntegrityError, OperationalError
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


# ── Retry decorator ───────────────────────────────────────────────────────────


def _compute_retry_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """Exponential back-off with jitter (capped at ``max_delay``)."""
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    return delay + delay * 0.1 * random.random()


def _check_db_error(exc, attempt: int, max_retries: int):
    """Raise a typed exception or return True (retry) / False (fatal)."""
    if isinstance(exc, OperationalError):
        if attempt >= max_retries:
            logger.error("DB transient error – exhausted retries.")
            raise DatabaseError(str(exc)) from exc
        return True  # retry
    if isinstance(exc, IntegrityError):
        raise DuplicateError(str(exc)) from exc
    if isinstance(exc, SAError):
        raise DatabaseError(str(exc)) from exc
    return False  # not a DB error, let the caller handle it


def retry_on_db_failure(
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
):
    """Decorator that retries the wrapped call on transient DB failures.

    Uses exponential backoff with jitter.  Re-raises if all retries are
    exhausted or if the error is non-transient (e.g. constraint violation).
    """
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except (OperationalError, IntegrityError, SAError) as exc:
                    last_exc = exc
                    if _check_db_error(exc, attempt, max_retries):
                        delay = _compute_retry_delay(attempt, base_delay, max_delay)
                        logger.warning(
                            "DB transient error (attempt %d/%d): %s.  Retrying in %.2fs…",
                            attempt, max_retries, exc, delay,
                        )
                        await asyncio.sleep(delay)
            raise DatabaseError(str(last_exc)) if last_exc else DatabaseError("Unknown error")

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (OperationalError, IntegrityError, SAError) as exc:
                    last_exc = exc
                    if _check_db_error(exc, attempt, max_retries):
                        delay = _compute_retry_delay(attempt, base_delay, max_delay)
                        logger.warning(
                            "DB transient error (attempt %d/%d): %s.  Retrying in %.2fs…",
                            attempt, max_retries, exc, delay,
                        )
                        time.sleep(delay)  # noqa: ASYNC100
            raise DatabaseError(str(last_exc)) if last_exc else DatabaseError("Unknown error")

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


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


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI/Quart-compatible async session dependency.

    Yields a session and ensures it is closed (and rolled back on error)
    when the caller exits.
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


@asynccontextmanager
async def async_db_session():
    """Async context manager that yields a committed (or rolled-back) session.

    Usage::

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


# ── Utility helpers ───────────────────────────────────────────────────────────


def map_integrity_error(func):
    """Decorator that catches SQLAlchemy ``IntegrityError`` and raises our
    ``DuplicateError`` instead.
    """
    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except IntegrityError as exc:
            raise DuplicateError(str(exc)) from exc

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except IntegrityError as exc:
            raise DuplicateError(str(exc)) from exc

    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper
