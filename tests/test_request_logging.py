"""
Request-logging middleware tests (Phase 12, ``empyrean.request`` logger).

The app-level ``before_request``/``after_request`` hooks must emit exactly one
INFO record per HTTP request carrying the method, the path (with NO query
string), the response status, and the duration in milliseconds — and must never
contain a request body, an ``Authorization`` header, or a query-string
credential. Records land on the dedicated ``empyrean.request`` logger so they
can be tuned or filtered independently of the app logger.

Conventions mirror the other HTTP suites: scenarios run through :func:`_run` on
a fresh event loop, and Redis is patched to the fail-open ``None`` client so
auth rate-limit buckets never block.
"""

from __future__ import annotations

import asyncio
import logging
import re

import pytest

from app import create_app
from models.base import async_engine

# The log line carries exactly these four ``key=value`` fields — nothing else.
_LOG_LINE_RE = re.compile(
    r"^method=\S+ path=\S+ status=\d+ duration_ms=\d+(\.\d+)?$"
)


@pytest.fixture(autouse=True)
def _fast_redis_down(monkeypatch):
    """Simulate an unreachable Redis fast (documented fail-open path)."""
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


def _request_records(caplog) -> list[logging.LogRecord]:
    """Return the log records emitted on the ``empyrean.request`` logger."""
    return [r for r in caplog.records if r.name == "empyrean.request"]


def test_health_request_emits_exactly_one_log_record(caplog):
    """One real request → exactly one record containing method/path/status/duration."""
    caplog.set_level(logging.INFO, logger="empyrean.request")

    async def _scenario():
        client = create_app().test_client()
        resp = await client.get("/health")
        assert resp.status_code == 200

    _run(_scenario())

    records = _request_records(caplog)
    assert len(records) == 1, f"expected 1 request-log record, got {len(records)}"
    msg = records[0].getMessage()
    assert _LOG_LINE_RE.fullmatch(msg), f"unexpected log line: {msg!r}"
    assert "method=GET" in msg
    assert "path=/health" in msg
    assert "status=200" in msg
    assert "duration_ms=" in msg


def test_log_record_never_contains_body_or_authorization(caplog):
    """The log line is exactly the four fields — no body, no auth token.

    A register request carries both a distinctive Authorization header and a
    distinctive body (which fails validation with 422 before the handler runs);
    neither may appear anywhere in the log line.
    """
    caplog.set_level(logging.INFO, logger="empyrean.request")
    token = "SECRET-ACCESS-TOKEN-9f2a"
    body_password = "SECRET-BODY-PASSWORD-77"

    async def _scenario():
        client = create_app().test_client()
        resp = await client.post(
            "/api/v1/auth/register",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "username": "loguser",
                "email": "not-an-email",  # fails validation → 422, handler never runs
                "password": body_password,
            },
        )
        assert resp.status_code == 422

    _run(_scenario())

    records = _request_records(caplog)
    assert len(records) == 1, f"expected 1 request-log record, got {len(records)}"
    msg = records[0].getMessage()
    # The line is *only* method/path/status/duration_ms — structurally excludes
    # any body or header content.
    assert _LOG_LINE_RE.fullmatch(msg), f"unexpected log line: {msg!r}"
    assert token not in msg
    assert body_password not in msg
    assert "method=POST" in msg
    assert "path=/api/v1/auth/register" in msg
    assert "status=422" in msg


def test_query_string_credentials_never_logged(caplog):
    """The logged path excludes the query string — a ?token= never appears."""
    caplog.set_level(logging.INFO, logger="empyrean.request")
    qs_secret = "QUERY-TOKEN-leak-check"

    async def _scenario():
        client = create_app().test_client()
        resp = await client.get(f"/health?token={qs_secret}")
        assert resp.status_code == 200

    _run(_scenario())

    records = _request_records(caplog)
    assert len(records) == 1
    msg = records[0].getMessage()
    assert qs_secret not in msg
    assert "path=/health" in msg
    assert "?" not in msg
