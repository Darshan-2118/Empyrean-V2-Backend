"""
Shared time utilities for API modules.

Exposes :func:`parse_iso_datetime`, the repo's single ISO-8601 query-param
parser: naive timestamps are treated as UTC, aware timestamps are converted to
UTC, and malformed input raises ``ValueError``. Extracted from
``api/readings.py`` so the export endpoint shares the exact contract with
``/readings/history`` instead of re-implementing it.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_iso_datetime(value: str | None, *, default: datetime) -> datetime:
    """Parse an ISO-8601 query param, falling back to ``default``.

    Treats naive timestamps as UTC; raises ``ValueError`` on malformed input.
    """
    if value is None:
        return default
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{value!r} is not a valid ISO-8601 datetime") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
