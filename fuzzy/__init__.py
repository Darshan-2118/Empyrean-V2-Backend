"""Tsukamoto fuzzy inference engine — public API.

Consumers (e.g. the Celery reader task) use ``from fuzzy.tsukamoto import
infer``; the package also re-exports the entrypoint here for convenience.
"""

from __future__ import annotations

from fuzzy.tsukamoto import fuzzy_score, infer

__all__ = ["fuzzy_score", "infer"]
