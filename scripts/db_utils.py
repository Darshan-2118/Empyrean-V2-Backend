"""Shared database helpers for the scripts/ tools.

The operational scripts each need an engine bound to the configured
``DATABASE_URL`` with the same connection options.  Building it here keeps
those copies in sync.
"""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from config import get_config


def make_engine() -> Engine:
    """Create an engine for the configured ``DATABASE_URL``.

    ``pool_pre_ping`` guards against serving stale pooled connections and
    ``connect_timeout`` keeps connection failures fast instead of hanging.
    """
    cfg = get_config()
    return create_engine(
        cfg.DATABASE_URL,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
