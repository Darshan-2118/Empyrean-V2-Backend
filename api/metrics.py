import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from quart import request

REQ_COUNT = Counter(
    "empyrean_http_requests_total",
    "HTTP requests by method, route, status",
    ["method", "route", "status"],
)
REQ_LATENCY = Histogram(
    "empyrean_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route", "status"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)


def register_metrics(app):
    # Idempotent — app.py and the test both call this on the same app instance.
    if getattr(app, "_empyrean_metrics_registered", False):
        return
    app._empyrean_metrics_registered = True

    @app.before_request
    async def _start_timer():
        request.scope["_empyrean_metrics_start"] = time.perf_counter()

    @app.after_request
    async def _observe(response):
        labels = (
            request.method,
            str(request.url_rule or "unknown"),
            response.status_code,
        )
        REQ_COUNT.labels(*labels).inc()
        REQ_LATENCY.labels(*labels).observe(
            time.perf_counter()
            - request.scope.get("_empyrean_metrics_start", time.perf_counter())
        )
        return response

    @app.route("/metrics")
    async def metrics():
        # A 2-tuple (body, content_type) would be parsed as a status code.
        return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}
