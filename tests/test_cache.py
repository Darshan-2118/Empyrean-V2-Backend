"""
Read-through cache coverage (Phase 13 gap).

``GET /readings/latest`` and ``GET /forecast`` are the two API endpoints that
serve from the Redis cache layer (:mod:`api.cache`) via ``cache_get_json`` /
``cache_set_json`` — every other endpoint degrades straight to the DB. Until
this module no test exercised that path, so a refactor that broke the cache
contract (wrong key, wrong TTL, hit served instead of DB) would have sailed
through the suite. Real Redis is not required: ``api.cache.get_client`` is
monkeypatched to a tiny in-memory fake mirroring the async ``get``/``setex``/
``delete`` contract, and ``api.rate_limit.get_client`` (a direct import) is
fail-opened to ``None`` so rate-limit buckets never block.

Conventions mirror the other HTTP suites: scenarios run through a fresh event
loop and the DB engine pool is disposed on the way out.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from api.jwt import create_access_token
from app import create_app
from models import Node, SensorReading, User
from models.base import AsyncSessionLocal, dispose_engines
from models.helpers import hash_password

API = "/api/v1"


class FakeRedis:
    """Minimal async Redis stand-in: ``get``/``setex``/``delete`` over a dict."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


@pytest.fixture(autouse=True)
def _rate_limit_fail_open(monkeypatch):
    """Keep the rate limiter off a real client (documented fail-open path)."""
    monkeypatch.setattr("api.rate_limit.get_client", lambda: None)


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _run(coro):
    """Run an async scenario on a fresh loop, disposing the async pool."""

    async def _wrapped():
        try:
            return await coro
        finally:
            await dispose_engines()

    return asyncio.run(_wrapped())


async def _create_user(username: str) -> User:
    async with AsyncSessionLocal() as session:
        user = User(
            username=username,
            email=f"{username}@test.local",
            password_hash=hash_password("test-pass-1", rounds=4),
            role="user",
            is_active=True,
            notification_prefs={},
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _seed_active_node_with_reading(node_id: str) -> None:
    """Persist an active node plus one recent reading (key fields only)."""
    async with AsyncSessionLocal() as session:
        session.add(Node(node_id=node_id, name="Cache", reading_interval=30, is_active=True))
        session.add(
            SensorReading(
                time=datetime.now(timezone.utc),
                node_id=node_id,
                pm25=10.0,
                aqi=50,
                aqi_category="Good",
                is_anomaly=False,
            )
        )
        await session.commit()


async def _cleanup(node_id: str, username: str) -> None:
    """Remove the rows this module committed (alert-first ordering not needed)."""
    async with AsyncSessionLocal() as session:
        await session.execute(delete(SensorReading).where(SensorReading.node_id == node_id))
        await session.execute(delete(Node).where(Node.node_id == node_id))
        await session.execute(delete(User).where(User.username == username))
        await session.commit()


def test_readings_latest_read_through_and_write_back(monkeypatch):
    """Latest serves the cached payload on hit, the DB on miss, and rewrites the cache."""
    fake = FakeRedis()
    monkeypatch.setattr("api.cache.get_client", lambda: fake)
    db_node = _unique("CACHEDB")
    user = None

    async def scenario():
        nonlocal user
        await _seed_active_node_with_reading(db_node)
        user = await _create_user(_unique("cacheuser"))
        headers = _auth_headers(create_access_token(user.id, user.role))
        # A distinctive cached payload whose node never exists in the DB — if the
        # response shows it, the endpoint served from the cache, not the DB.
        cached_payload = [
            {
                "node_id": "CACHE-ONLY",
                "time": "2026-08-11T00:00:00Z",
                "temperature": 21.5,
                "humidity": 44.0,
                "pm25": 7.0,
                "pm10": 12.0,
                "aqi": 42,
                "aqi_category": "Good",
                "is_anomaly": False,
            }
        ]
        fake.data["readings:latest"] = json.dumps(cached_payload)

        async with create_app().test_client() as client:
            # Cache HIT → the CACHE-ONLY node is returned; the DB node is not.
            hit = await client.get(f"{API}/readings/latest", headers=headers)
            assert hit.status_code == 200
            assert (await hit.get_json())["readings"] == cached_payload
            assert all(r["node_id"] != db_node for r in (await hit.get_json())["readings"])

            # Evict the cache → the next request reads from the DB and rewrites it.
            fake.data.clear()
            miss = await client.get(f"{API}/readings/latest", headers=headers)
            assert miss.status_code == 200
            miss_body = (await miss.get_json())["readings"]
            assert any(r["node_id"] == db_node for r in miss_body)
            # Write-back under the same key with the DB payload.
            assert "readings:latest" in fake.data
            assert json.loads(fake.data["readings:latest"]) == miss_body

    try:
        _run(scenario())
    finally:
        if user is not None:
            _run(_cleanup(db_node, user.username))


def test_forecast_serves_from_cache(monkeypatch):
    """Forecast honors ``celery:forecast:{node_id}`` and skips computation on a hit."""
    fake = FakeRedis()
    monkeypatch.setattr("api.cache.get_client", lambda: fake)
    node_id = _unique("CACHEFC")
    user = None

    async def scenario():
        nonlocal user
        await _seed_active_node_with_reading(node_id)
        user = await _create_user(_unique("cachefcuser"))
        headers = _auth_headers(create_access_token(user.id, user.role))
        # A distinctive aqi the on-the-fly generator could not produce from an
        # empty node — presence proves the cached points were served verbatim.
        fake.data[f"celery:forecast:{node_id}"] = json.dumps(
            [{"time": "2026-08-11T00:00:00Z", "aqi": 123}]
        )

        async with create_app().test_client() as client:
            resp = await client.get(f"{API}/forecast?node_id={node_id}", headers=headers)
            assert resp.status_code == 200
            body = await resp.get_json()
            assert body["node_id"] == node_id
            assert body["horizon_minutes"] == 60
            assert len(body["points"]) == 1
            assert body["points"][0]["aqi"] == 123
            # The cached key was NOT rewritten (a hit is read-only).
            assert json.loads(fake.data[f"celery:forecast:{node_id}"])[0]["aqi"] == 123

    try:
        _run(scenario())
    finally:
        if user is not None:
            _run(_cleanup(node_id, user.username))
