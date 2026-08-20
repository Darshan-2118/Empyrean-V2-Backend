"""Shared utility helpers used across models, seed scripts, and tests."""

import asyncio
import bcrypt


def hash_password(password: str, rounds: int = 12) -> str:
    """Return a bcrypt hash of *password*.

    Args:
        password: Plain-text password to hash.
        rounds:  bcrypt cost factor (4–12).  Use 4 in tests for speed,
                12 (default) in seed/production.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


async def get_request_session():
    """Get the request-scoped async session created by the middleware (#10).

    Returns the session for the current Quart request, or None if there is no
    active request context. This allows routes to access `request.session`
    using asynchronous code.
    """
    from quart import request
    from models.base import AsyncSessionLocal

    if not asyncio.current_task():
        return None

    try:
        # Try to get the session factory from the Flask context
        factory = getattr(request.app.g, "request_session_factory", None)
        if not factory:
            return None

        # Return the factory, allowing the route to lazy-create the session
        return lambda: asyncio.create_task(factory().__aenter__())
    except RuntimeError:
        # No request context (e.g., from background tasks)
        return None
