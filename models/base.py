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
import time
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import AsyncGenerator, Generator

from sqlalchemy import MetaData, create_engine
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

# Build an async-compatible URL (replace postgresql:// → postgresql+asyncpg://)
_async_db_url = _sync_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

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
                except OperationalError as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        jitter = delay * 0.1 * (time.time() % 1)
                        logger.warning(
                            "DB transient error (attempt %d/%d): %s.  Retrying in %.2fs…",
                            attempt, max_retries, exc, delay + jitter,
                        )
                        await asyncio.sleep(delay + jitter)
                    else:
                        logger.error("DB transient error – exhausted retries.")
                        raise DatabaseError(str(exc)) from exc
                except IntegrityError as exc:
                    raise DuplicateError(str(exc)) from exc
                except SAError as exc:
                    raise DatabaseError(str(exc)) from exc
            # Should not reach here, but belt-and-suspenders
            raise DatabaseError(str(last_exc)) if last_exc else DatabaseError("Unknown error")

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except OperationalError as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        jitter = delay * 0.1 * (time.time() % 1)
                        logger.warning(
                            "DB transient error (attempt %d/%d): %s.  Retrying in %.2fs…",
                            attempt, max_retries, exc, delay + jitter,
                        )
                        time.sleep(delay + jitter)  # noqa: ASYNC100 — sync wrapper is deliberate
                    else:
                        logger.error("DB transient error – exhausted retries.")
                        raise DatabaseError(str(exc)) from exc
                except IntegrityError as exc:
                    raise DuplicateError(str(exc)) from exc
                except SAError as exc:
                    raise DatabaseError(str(exc)) from exc
            raise DatabaseError(str(last_exc)) if last_exc else DatabaseError("Unknown error")

        # Return the right wrapper based on whether the original was a coroutine
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


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
