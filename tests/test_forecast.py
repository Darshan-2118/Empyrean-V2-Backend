"""Regression tests for the three forecast-path fixes (L-10, L-11, L-29).

* **L-10** — a Redis-cached model blob that is valid JSON but has a non-numeric
  or non-finite ``slope``/``intercept`` (or is not a dict at all) must be
  rejected by ``_get_model`` (returns ``None``) instead of flowing through to
  ``slope * ts.timestamp() + intercept`` and 500ing the API route.
* **L-11** — ``retrain_model`` must invalidate the *served* forecast key
  ``celery:forecast:{node_id}`` after writing a fresh model, so stale served
  forecasts do not survive a retrain; the delete is fail-soft in its own block.
* **L-29** — ``GET /forecast`` for an *inactive* node must return 404 (it is
  treated like an unknown node).

Redis is optional/fail-open in this app, so the Redis-path tests stand in
``tasks.forecast._redis()`` with a lightweight fake and never touch a broker.
The HTTP test runs the real async app against the test DB (committed rows via
the sync pipeline), reusing the phase-coverage conventions.
"""

from __future__ import annotations

import asyncio
import json
import secrets

import tasks.forecast as fc
from app import create_app
from api.jwt import create_access_token
from models import Node, User
from models.base import async_engine, get_sync_db
from models.helpers import hash_password


# ── Fake Redis (recording) ────────────────────────────────────────────────────


class _StubRedis:
    """Minimal Redis stand-in for the forecast task's sync calls.

    ``get`` serves a canned model blob; ``setex``/``delete`` record their
    calls so tests can assert on the write and invalidation side effects
    without a broker.
    """

    def __init__(self, model_blob=None):
        self.model_blob = model_blob
        self.sets: list[tuple] = []
        self.deletes: list[str] = []

    def get(self, key: str) -> str | None:
        if key.startswith("forecast:model:"):
            return self.model_blob
        return None

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.sets.append((key, ttl, value))

    def delete(self, key: str) -> None:
        self.deletes.append(key)


class _FakeScalarResult:
    def all(self):
        return ["N"]


class _FakeSession:
    def scalars(self, _stmt):
        return _FakeScalarResult()


class _FakeDB:
    """Context manager standing in for ``models.base.get_sync_db()``."""

    def __enter__(self):
        return _FakeSession()

    def __exit__(self, *exc):
        return False


# ── L-10: corrupted / wrong-type model blobs are rejected ─────────────────────


def test_get_model_rejects_wrong_type_slope(monkeypatch):
    """L-10: a valid-JSON blob with a non-numeric ``slope`` is rejected."""
    blob = json.dumps({"slope": "foo", "intercept": 1})
    monkeypatch.setattr(fc, "_redis", lambda: _StubRedis(model_blob=blob))
    assert fc._get_model("N") is None


def test_get_model_rejects_non_dict(monkeypatch):
    """L-10: a JSON blob that is not an object (e.g. ``42``) is rejected."""
    monkeypatch.setattr(fc, "_redis", lambda: _StubRedis(model_blob="42"))
    assert fc._get_model("N") is None


def test_get_model_returns_valid_dict(monkeypatch):
    """L-10: a well-formed finite model dict passes through unchanged."""
    blob = json.dumps({"slope": 2.0, "intercept": -5.0})
    monkeypatch.setattr(fc, "_redis", lambda: _StubRedis(model_blob=blob))
    assert fc._get_model("N") == {"slope": 2.0, "intercept": -5.0}


# ── L-11: retrain invalidates the served forecast key ─────────────────────────


def test_retrain_model_invalidates_served_forecast(monkeypatch):
    """L-11: after writing a fresh model, retrain deletes the stale served
    ``celery:forecast:{node_id}`` key (fail-soft, logged only)."""
    redis = _StubRedis()
    monkeypatch.setattr(fc, "_redis", lambda: redis)
    monkeypatch.setattr(
        fc,
        "_fit_model",
        lambda points: {"slope": 2.0, "intercept": -5.0, "trained_at": "2026-08-05T00:00:00Z"},
    )
    monkeypatch.setattr(fc, "_training_points", lambda node_id: [(float(i), float(i)) for i in range(40)])
    monkeypatch.setattr(fc, "get_sync_db", lambda: _FakeDB())

    result = fc.retrain_model()

    assert result == {"models": 1}
    assert any(key == "forecast:model:N" for key, _ttl, _val in redis.sets)
    assert redis.deletes == ["celery:forecast:N"]


# ── L-29: inactive node → 404 ─────────────────────────────────────────────────


def _run_async(coro):
    async def _wrapped():
        try:
            return await coro
        finally:
            await async_engine.dispose()

    return asyncio.run(_wrapped())


def test_inactive_node_forecast_returns_404():
    """L-29: the forecast route requires an active node; an inactive one is a
    clean 404 (treated like an unknown node_id)."""

    async def _scenario():
        tag = secrets.token_hex(3)
        username = f"p29_{tag}"
        node_id = f"INACTIVE-{tag}"
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
                name="inactive node",
                location_name="Test Lab",
                lat=0.0,
                lon=0.0,
                reading_interval=30,
                is_active=False,  # deliberately inactive → must 404
            )
            session.add(node)
            session.flush()
            user_id = user.id

        client = create_app().test_client()
        headers = {"Authorization": f"Bearer {create_access_token(user_id, 'user')}"}
        resp = await client.get(f"/api/v1/forecast?node_id={node_id}", headers=headers)
        assert resp.status_code == 404
        err = await resp.get_json()
        assert err["status"] == 404

    _run_async(_scenario())