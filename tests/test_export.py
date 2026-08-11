"""
Export-phase tests — CSV streaming, filtering, validation, and headers.

Focused tests for Phase 11 (``GET /api/v1/export``): header-first CSV structure
and cell formatting (including a direct, DB-free unit test of ``_csv_chunks``
over fake rows), node+range filtering with inclusive bounds, the 422 validation
gates (malformed from/to, ``from >= to``, span-exceed), the anonymous 401 with
rate-limit headers attached, and the Content-Type / Content-Disposition /
``X-RateLimit-*`` response headers.

Rows are seeded through the sync ``get_sync_db()`` pipeline (committed) with
unique node ids, following the ``test_phase_coverage`` pattern — no cleanup is
needed because conftest drops all tables at session end and the ids never
collide across modules.
"""

from __future__ import annotations

import asyncio
import csv
import io
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import create_app
from api.export import _CHUNK_BYTES, _CSV_COLUMNS, _csv_chunks, _format_cell
from api.jwt import create_access_token
from models import Node, SensorReading, User
from models.base import async_engine, get_sync_db
from models.helpers import hash_password


@pytest.fixture(autouse=True)
def _fast_redis_down(monkeypatch):
    """Simulate an unreachable Redis fast (documented fail-open path).

    Mirrors ``tests/test_api.py``: patching the cache/rate-limit client to
    ``None`` exercises the fail-open branch (rate-limit headers still attached)
    without blocking on the OS connect timeout.
    """
    monkeypatch.setattr("api.rate_limit.get_client", lambda: None)
    monkeypatch.setattr("api.cache.get_client", lambda: None)


def _run(coro):
    """Run an async scenario on a fresh loop, then dispose the async pool."""

    async def _wrapped():
        try:
            return await coro
        finally:
            await async_engine.dispose()

    return asyncio.run(_wrapped())


def _unique(prefix: str) -> str:
    """Return a unique node-id slug (committed rows must never collide)."""
    return f"{prefix.upper()}-{secrets.token_hex(4).upper()}"


def _seed_user_and_node(prefix: str) -> tuple[int, str]:
    """Create (committed) a user + active node via the sync pipeline."""
    tag = secrets.token_hex(3)
    username = f"{prefix}_{tag}"
    node_id = f"{prefix.upper()}-{tag}"
    with get_sync_db() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("secret-pass-123", rounds=4),
            role="user",
            is_active=True,
            notification_prefs={},
        )
        session.add(user)
        node = Node(
            node_id=node_id,
            name=f"{prefix} node",
            location_name="Test Lab",
            lat=28.6139,
            lon=77.2090,
            firmware_version="v2.1.0",
            reading_interval=30,
            is_active=True,
        )
        session.add(node)
        session.flush()
        user_id = user.id
    return user_id, node_id


def _seed_reading(node_id: str, ts: datetime, **kw) -> None:
    """Persist one committed SensorReading row for ``node_id`` at ``ts``."""
    fields = {
        "temperature": 22.0,
        "humidity": 50.0,
        "pressure": 1012.3,
        "voc_ohm": 10000.0,
        "mq135_ppm": 12.5,
        "pm1": 5.0,
        "pm25": 35.0,
        "pm10": 60.0,
        "battery_v": 3.9,
        "fuzzy_score": 42.0,
        "aqi": 50,
        "aqi_category": "Good",
        "is_anomaly": False,
    }
    fields.update(kw)
    with get_sync_db() as session:
        session.add(SensorReading(node_id=node_id, time=ts, **fields))


def _iso_utc(dt: datetime) -> str:
    """ISO-8601 UTC string with ``Z`` suffix (safe in a query string)."""
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── CSV generator unit tests (DB-free) ───────────────────────────────────────


def test_format_cell():
    """Cell formatting: None → empty, bool → lowercase, numerics via str()."""
    assert _format_cell(None) == ""
    assert _format_cell(True) == "true"
    assert _format_cell(False) == "false"
    assert _format_cell(42.0) == "42.0"  # float renders with a decimal point
    assert _format_cell(101) == "101"    # int renders bare
    assert _format_cell("Moderate") == "Moderate"
    assert _format_cell(0) == "0"  # int(0) must not hit the bool branch


def _fake_row(i: int) -> SimpleNamespace:
    """A fake SensorReading-shaped object (no DB needed)."""
    return SimpleNamespace(
        time=datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc),
        node_id="NODE-X",
        temperature=22.5,
        humidity=50.0,
        pressure=1012.3,
        voc_ohm=10000.0,
        mq135_ppm=12.5,
        pm1=5.0,
        pm25=35.0,
        pm10=60.0,
        battery_v=3.9,
        fuzzy_score=42.0,
        aqi=101,
        aqi_category="Unhealthy for Sensitive Groups",
        is_anomaly=(i % 2 == 0),
    )


def test_csv_chunks_header_first_empty_source():
    """``_csv_chunks`` over an empty source yields exactly the header row."""

    async def _empty_rows():
        return
        yield None  # pragma: no cover — makes this an async generator

    async def _collect():
        return "".join([c async for c in _csv_chunks(_empty_rows())])

    body = asyncio.run(_collect())
    assert body == "time,node_id,temperature,humidity,pressure,voc_ohm,mq135_ppm,pm1,pm25,pm10,battery_v,fuzzy_score,aqi,aqi_category,is_anomaly\r\n"


def test_csv_chunks_header_first_and_bounded_chunking():
    """Many rows stream as header-first CSV in ~64KB chunks."""

    async def _rows(n: int):
        for i in range(n):
            yield _fake_row(i)

    async def _collect(n: int):
        chunks = []
        async for chunk in _csv_chunks(_rows(n)):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect(2000))
    assert len(chunks) >= 2, "2000 rows must exceed the first 64KB chunk"

    body = "".join(chunks)
    parsed = list(csv.reader(io.StringIO(body)))
    assert parsed[0] == _CSV_COLUMNS  # header first
    assert len(parsed) == 2001  # header + one row per fake row

    # Bounded chunking: no single chunk is more than one row over the cap.
    assert max(len(c) for c in chunks) <= _CHUNK_BYTES + 4096

    # Spot-check cell formatting on the first data row (a fake with is_anomaly
    # True, then one with False).
    even = next(r for r in parsed[1:] if r[14] == "true")
    assert even[0].endswith("Z") and "+" not in even[0]
    assert even[1] == "NODE-X"
    assert even[12] == "101"  # aqi int
    assert even[13] == "Unhealthy for Sensitive Groups"
    odd = next(r for r in parsed[1:] if r[14] == "false")
    assert odd[14] == "false"


# ── HTTP contract ─────────────────────────────────────────────────────────────


def test_export_csv_structure_and_headers():
    """GET /export returns header-first CSV with the documented headers."""

    async def _scenario():
        user_id, node_id = _seed_user_and_node("exp")
        base = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
        _seed_reading(node_id, base, temperature=25.5, aqi=101,
                      aqi_category="Unhealthy for Sensitive Groups", is_anomaly=True)
        _seed_reading(node_id, base + timedelta(minutes=30))

        client = create_app().test_client()
        headers = _auth(create_access_token(user_id, "user"))
        url = (
            f"/api/v1/export?node_id={node_id}"
            f"&from={_iso_utc(base - timedelta(minutes=1))}"
            f"&to={_iso_utc(base + timedelta(minutes=31))}"
        )
        resp = await client.get(url, headers=headers)
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        assert resp.content_type == "text/csv; charset=utf-8"
        assert resp.headers.get(
            "Content-Disposition"
        ) == 'attachment; filename="readings_export_20260810T115900Z_20260810T123100Z.csv"'
        assert resp.headers.get("Cache-Control") == "no-store"
        # rate-limit headers attached on success
        assert resp.headers.get("X-RateLimit-Limit") == "200"
        assert resp.headers.get("X-RateLimit-Remaining") is not None
        assert resp.headers.get("X-RateLimit-Reset") is not None

        body = (await resp.get_data()).decode("utf-8")
        rows = list(csv.reader(io.StringIO(body)))
        assert rows[0] == _CSV_COLUMNS
        assert len(rows) == 3  # header + 2 readings
        anomaly = rows[1]
        assert anomaly[1] == node_id
        assert anomaly[0] == "2026-08-10T12:00:00Z"  # trailing Z, repo contract
        assert anomaly[2] == "25.5"   # temperature float
        assert anomaly[12] == "101"   # aqi int
        assert anomaly[13] == "Unhealthy for Sensitive Groups"  # quoted? unquoted here
        assert anomaly[14] == "true"  # bool lowercased
        assert rows[2][14] == "false"

    _run(_scenario())


def test_export_node_and_range_filtering_inclusive():
    """Rows exactly at ``from``/``to`` are included; node_id filters the set."""

    async def _scenario():
        user_id, node_a = _seed_user_and_node("filt")
        _, node_b = _seed_user_and_node("filtb")
        base = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(3):  # t0, t0+30m, t0+60m on node A
            _seed_reading(node_a, base + timedelta(minutes=i * 30))
        _seed_reading(node_b, base + timedelta(minutes=30))  # node B at t1

        client = create_app().test_client()
        headers = _auth(create_access_token(user_id, "user"))

        # Wide range on node A → all 3 rows.
        resp = await client.get(
            f"/api/v1/export?node_id={node_a}"
            f"&from={_iso_utc(base)}&to={_iso_utc(base + timedelta(minutes=60))}",
            headers=headers,
        )
        assert resp.status_code == 200
        rows = list(csv.reader(io.StringIO((await resp.get_data()).decode())))
        assert len(rows) == 4  # header + 3
        assert all(r[1] == node_a for r in rows[1:])

        # Narrow range [t1, t2]: both boundary rows are inclusive.
        resp = await client.get(
            f"/api/v1/export?node_id={node_a}"
            f"&from={_iso_utc(base + timedelta(minutes=30))}"
            f"&to={_iso_utc(base + timedelta(minutes=60))}",
            headers=headers,
        )
        rows = list(csv.reader(io.StringIO((await resp.get_data()).decode())))
        assert len(rows) == 3  # header + t1 + t2 (time == from and time == to)
        assert rows[1][0] == "2026-08-10T12:30:00Z"
        assert rows[2][0] == "2026-08-10T13:00:00Z"

        # node_id filter keeps node A's rows only.
        resp = await client.get(
            f"/api/v1/export?node_id={node_b}"
            f"&from={_iso_utc(base)}&to={_iso_utc(base + timedelta(minutes=60))}",
            headers=headers,
        )
        rows = list(csv.reader(io.StringIO((await resp.get_data()).decode())))
        assert len(rows) == 2  # header + node B's single row
        assert rows[1][1] == node_b

        # No rows in range → header only, still 200.
        resp = await client.get(
            f"/api/v1/export?node_id={node_a}"
            f"&from={_iso_utc(base + timedelta(days=1))}"
            f"&to={_iso_utc(base + timedelta(days=1, minutes=30))}",
            headers=headers,
        )
        assert resp.status_code == 200
        assert (await resp.get_data()).decode() == (
            "time,node_id,temperature,humidity,pressure,voc_ohm,mq135_ppm,pm1,pm25,pm10,battery_v,fuzzy_score,aqi,aqi_category,is_anomaly\r\n"
        )

    _run(_scenario())


def test_export_validation_errors_422():
    """Malformed from/to, from>=to, and span-exceed all 422 before streaming."""

    async def _scenario():
        user_id, _ = _seed_user_and_node("val")
        client = create_app().test_client()
        headers = _auth(create_access_token(user_id, "user"))

        # malformed from → 422, detail = parser's ValueError message
        resp = await client.get("/api/v1/export?from=not-a-date", headers=headers)
        assert resp.status_code == 422
        err = await resp.get_json()
        assert err["title"] == "Unprocessable Entity"
        assert "not-a-date" in err["detail"]

        # malformed to → 422
        resp = await client.get("/api/v1/export?to=garbage", headers=headers)
        assert resp.status_code == 422
        assert (await resp.get_json())["status"] == 422

        # from >= to → 422
        resp = await client.get(
            f"/api/v1/export?from={_iso_utc(datetime.now(timezone.utc))}"
            f"&to={_iso_utc(datetime.now(timezone.utc) - timedelta(hours=1))}",
            headers=headers,
        )
        assert resp.status_code == 422
        assert "'from' must be earlier than 'to'" in (await resp.get_json())["detail"]

        # span over MAX_EXPORT_SPAN (365 days) → 422 naming the cap
        now = datetime.now(timezone.utc)
        resp = await client.get(
            f"/api/v1/export?from={_iso_utc(now - timedelta(days=400))}"
            f"&to={_iso_utc(now)}",
            headers=headers,
        )
        assert resp.status_code == 422
        assert "365 days" in (await resp.get_json())["detail"]

    _run(_scenario())


def test_export_anonymous_401_with_rate_headers():
    """No token → 401 problem+json, but X-RateLimit-* headers still attached."""

    async def _scenario():
        client = create_app().test_client()
        resp = await client.get("/api/v1/export")
        assert resp.status_code == 401
        body = await resp.get_json()
        assert body["status"] == 401
        assert "application/problem+json" in resp.headers.get("Content-Type", "")
        assert resp.headers.get("X-RateLimit-Limit") == "200"
        assert resp.headers.get("X-RateLimit-Remaining") is not None
        assert resp.headers.get("X-RateLimit-Reset") is not None

    _run(_scenario())
