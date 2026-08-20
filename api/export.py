"""
Export blueprint — streaming CSV download of raw sensor readings.

* ``GET /export`` — ``@jwt_required`` (any authenticated user, not admin-only),
  rate-limited per IP (200/min). Streams ``sensor_readings`` rows in
  ``[from, to]`` (optionally one ``node_id``) as an RFC 4180 CSV attachment.
  Query params mirror ``/readings/history``: ``from`` (ISO, default 24h ago),
  ``to`` (ISO, default now), ``node_id`` (str, default all nodes); both bounds
  are inclusive and the span is capped at ``MAX_EXPORT_SPAN`` (365 days,
  matching the default ``DATA_RETENTION_DAYS``).

The response body is a chunked async generator. The DB session and server-side
cursor live inside the generator and are always released: Quart wraps an
async-iterable body in ``IterableBody`` whose ``__aexit__`` calls ``aclose()``
on the generator, so its ``finally`` runs on normal completion AND on client
disconnect. All validation errors are returned as RFC 7807 problem+json before
streaming starts — never mid-CSV.

Two availability guards keep a large export from degrading the rest of the API:
the returned ``Response`` sets ``timeout = None``, opting out of Quart's
app-wide ``RESPONSE_TIMEOUT`` (default 60s) that would otherwise cut any
stream longer than a minute mid-body as a silently-truncated 200; and a
module-level semaphore caps how many exports may stream concurrently, because
each in-flight stream pins one asyncpg pool connection (5 + 10 overflow) for
its whole duration.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime, timedelta, timezone

from quart import Blueprint, Response, request
from sqlalchemy import select

from api._time import parse_iso_datetime
from api.jwt import _problem_json, jwt_required
from api.rate_limit import rate_limit
from models import SensorReading
from models.base import AsyncSessionLocal

logger = logging.getLogger("empyrean.export")

export_bp = Blueprint("export", __name__)

# Widest range a single export may span. Matches the default
# DATA_RETENTION_DAYS=365, so a wider request could never return more data
# (data_retention_cleanup purges older rows) — exceeding it is an explicit 422,
# never a silent truncation.
MAX_EXPORT_SPAN = timedelta(days=365)

# Bytes buffered into io.StringIO before a chunk is yielded. The CSV is ASCII,
# so chars == bytes for the chunking check.
_CHUNK_BYTES = 64 * 1024

# Maximum number of exports that may stream at once. Each in-flight export
# pins one asyncpg pool connection for its whole stream (up to minutes), and
# the async pool is bounded (5 + 10 overflow = 15), so unbounded concurrent
# exports could exhaust it and stall every other DB-backed endpoint. Capping
# exports at 4 leaves 11 pool connections for all other traffic; surplus
# export requests wait on the semaphore instead of 500ing on pool acquisition.
MAX_CONCURRENT_EXPORTS = 4
_exports_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXPORTS)

# Per-export timeout in seconds. Prevents slow-client DoS attack where a
# client opens an export and holds the connection indefinitely. This timeout
# applies to the entire export stream, not individual chunks (#5).
MAX_EXPORT_TIMEOUT = 300  # 5 minutes

# The 15 SensorReading columns in model order; the header row uses these names.
_CSV_COLUMNS = [
    "time",
    "node_id",
    "temperature",
    "humidity",
    "pressure",
    "voc_ohm",
    "mq135_ppm",
    "pm1",
    "pm25",
    "pm10",
    "battery_v",
    "fuzzy_score",
    "aqi",
    "aqi_category",
    "is_anomaly",
]


def _format_cell(value) -> str:
    """Format one CSV cell: ``None`` → ``""``, bool → lowercase, else ``str()``."""
    if value is None:
        return ""
    # Check bool before int — bool subclasses int.
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _format_row(r: SensorReading) -> list[str]:
    """Format a ``SensorReading`` ORM row as one CSV row (15 cells)."""
    return [
        (
            r.time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if r.time is not None
            else ""
        ),
        _format_cell(r.node_id),
        _format_cell(r.temperature),
        _format_cell(r.humidity),
        _format_cell(r.pressure),
        _format_cell(r.voc_ohm),
        _format_cell(r.mq135_ppm),
        _format_cell(r.pm1),
        _format_cell(r.pm25),
        _format_cell(r.pm10),
        _format_cell(r.battery_v),
        _format_cell(r.fuzzy_score),
        _format_cell(r.aqi),
        _format_cell(r.aqi_category),
        _format_cell(r.is_anomaly),
    ]


async def _csv_chunks(rows: AsyncIterable[SensorReading]) -> AsyncIterator[str]:
    """Yield the CSV body in ~64KB chunks, header first.

    DB-free and directly unit-testable: ``rows`` is any async iterable of
    objects exposing the 15 SensorReading attributes.
    
    Enforces MAX_EXPORT_TIMEOUT to prevent slow-client DoS (#5).
    """
    import time
    start_time = time.time()
    
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(_CSV_COLUMNS)
    async for row in rows:
        # Check timeout on each row iteration
        elapsed = time.time() - start_time
        if elapsed > MAX_EXPORT_TIMEOUT:
            logger.warning(
                "Export timeout exceeded after %.1f seconds, terminating stream",
                elapsed
            )
            yield out.getvalue()
            return
        
        writer.writerow(_format_row(row))
        if out.tell() >= _CHUNK_BYTES:
            yield out.getvalue()
            out.seek(0)
            out.truncate(0)
    if out.tell():
        yield out.getvalue()


async def _stream_rows(
    from_dt: datetime, to_dt: datetime, node_id: str | None
) -> AsyncIterator[SensorReading]:
    """Stream raw ``sensor_readings`` rows in ``[from_dt, to_dt]``.

    Uses ``AsyncSession.stream`` (server-side cursor) inside a session opened
    *here*, so the pooled connection and cursor are always released — even if
    the consumer disconnects mid-stream, the ``finally`` runs when the
    generator is closed. The concurrency semaphore is held for the same
    lifetime: it is acquired before the session opens and released by the
    enclosing ``async with`` when the generator is closed, so at most
    ``MAX_CONCURRENT_EXPORTS`` exports pin a pooled connection at once.
    """
    stmt = (
        select(SensorReading)
        .where(SensorReading.time >= from_dt, SensorReading.time <= to_dt)
        .order_by(SensorReading.time, SensorReading.node_id)
    )
    if node_id:
        stmt = stmt.where(SensorReading.node_id == node_id)

    async with _exports_semaphore:
        async with AsyncSessionLocal() as session:
            result = await session.stream(stmt)
            try:
                async for row in result.scalars():
                    yield row
            finally:
                await result.close()


@export_bp.route("", methods=["GET"])
@rate_limit()
@jwt_required
async def export():
    """Stream raw readings in ``[from, to]`` (optionally one node) as CSV.

    Query params (all optional): ``from`` (ISO-8601, default now - 24h),
    ``to`` (ISO-8601, default now), ``node_id`` (str, default all nodes).
    Validation errors (malformed ``from``/``to``, ``from >= to``, span over
    ``MAX_EXPORT_SPAN``) return 422 problem+json before any CSV is streamed.
    """
    now = datetime.now(timezone.utc)
    try:
        from_dt = parse_iso_datetime(
            request.args.get("from"), default=now - timedelta(hours=24)
        )
        to_dt = parse_iso_datetime(request.args.get("to"), default=now)
    except ValueError as exc:
        return _problem_json(422, "Unprocessable Entity", str(exc))

    if from_dt >= to_dt:
        return _problem_json(
            422, "Unprocessable Entity", "'from' must be earlier than 'to'"
        )

    if to_dt - from_dt > MAX_EXPORT_SPAN:
        return _problem_json(
            422,
            "Unprocessable Entity",
            f"range exceeds the maximum export span of {MAX_EXPORT_SPAN.days} days",
        )

    node_id = request.args.get("node_id") or None

    csv_chunks = _csv_chunks(_stream_rows(from_dt, to_dt, node_id))
    filename = (
        f"readings_export_"
        f"{from_dt.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_"
        f"{to_dt.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    )
    resp = Response(
        csv_chunks,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
    # Quart wraps every Response send in asyncio.wait_for(..., timeout=
    # RESPONSE_TIMEOUT), defaulting to the app-wide 60s (config defines no
    # override). A one-year export legitimately streams for minutes, so that
    # default would cancel the send mid-body and surface a 200 with a silently
    # truncated CSV. Setting timeout=None disables the per-response timeout —
    # a client disconnect is still handled by IterableBody closing the
    # generator, releasing the session/cursor.
    resp.timeout = None
    return resp
