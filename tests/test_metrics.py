"""
Prometheus /metrics endpoint tests (Phase 14).

The middleware records a REQ_COUNT / REQ_LATENCY sample per HTTP request and
exposes them at /metrics in the Prometheus text exposition format. The test
registers the middleware explicitly so it runs standalone; register_metrics()
is idempotent, so once app.py wires it too the double registration is a no-op.
Assertions are tolerant of counters accumulated by other tests in the run.
"""

import pytest

from app import create_app


@pytest.fixture(autouse=True)
def _fast_redis_down(monkeypatch):
    """Simulate an unreachable Redis fast (documented fail-open path)."""
    monkeypatch.setattr("api.rate_limit.get_client", lambda: None)
    monkeypatch.setattr("api.cache.get_client", lambda: None)


@pytest.mark.asyncio
async def test_health_then_metrics():
    from api.metrics import register_metrics

    app = create_app()
    register_metrics(app)

    async with app.test_client() as client:
        health = await client.get("/health")
        assert health.status_code == 200

        metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert metrics.headers["Content-Type"].startswith("text/plain")
        body = (await metrics.get_data()).decode()
        assert "empyrean_http_requests_total" in body
        assert 'status="200"' in body
        assert 'route="/health"' in body
