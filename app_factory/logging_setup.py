"""One-shot logging configuration (M7).

``logging.basicConfig`` adds a root handler. Calling it once per
``create_app()`` (as the original factory did) re-attaches duplicate handlers
on every app construction — under tests and multi-worker deployments the same
record gets logged many times. This module configures logging exactly once per
process, guarded by a module-level sentinel, so the config is always idempotent
no matter how many ``create_app()`` calls happen.
"""

from __future__ import annotations

import logging
import sys

_configured = False


def setup_logging(level: int | str = logging.INFO, fmt: str | None = None) -> None:
    """Configure root logging once per process.

    Subsequent calls are no-ops, so ``create_app()`` can call this freely
    without duplicating handlers. ``level``/``fmt`` only take effect on the
    first call; changing them later is ignored by design.
    """
    global _configured
    if _configured:
        return
    _configured = True

    logging.basicConfig(
        level=level,
        format=fmt
        or "%(asctime)s  %(levelname)-8s  %(name)-16s  %(message)s",
        stream=sys.stdout,
    )
