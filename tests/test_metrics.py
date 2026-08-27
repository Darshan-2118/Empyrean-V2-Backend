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


@pytest.mark.asyncio
async def test_unmatched_route_uses_bounded_unknown_sentinel():
    """M33: a request that matches no route is labelled ``route="unknown"``.

    The sentinel keeps the label set bounded — pinning it here means a refactor
    that starts labelling 404s with the raw request path (a client-controlled
    cardinality blowup) cannot sneak back in.
    """
    from api.metrics import register_metrics

    app = create_app()
    register_metrics(app)

    async with app.test_client() as client:
        miss = await client.get("/definitely-not-a-route-m33")
        assert miss.status_code == 404

        metrics = await client.get("/metrics")
        body = (await metrics.get_data()).decode()
        assert 'route="unknown"' in body
        # The raw path must never become a series label.
        assert "/definitely-not-a-route-m33" not in body
