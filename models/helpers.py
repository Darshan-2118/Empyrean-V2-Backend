"""Shared utility helpers used across models, seed scripts, and tests."""

import bcrypt


def hash_password(password: str, rounds: int = 12) -> str:
    """Return a bcrypt hash of *password*.

    Args:
        password: Plain-text password to hash.
        rounds:  bcrypt cost factor (4–12).  Use 4 in tests for speed,
                12 (default) in seed/production.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")
