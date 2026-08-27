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
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 10],
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
        # L20: the route label is the URL *rule* (e.g. "/api/v1/nodes/<node_id>"),
        # never the request path, so client input can't mint new series —
        # unmatched routes collapse to the bounded "unknown" sentinel (pinned
        # by tests/test_metrics.py, M33). Cardinality caveat: a future route
        # template embedding a regex converter would widen the label set;
        # keep route rules literal.
        route = str(request.url_rule or "unknown")

        labels = (
            request.method,
            route,
            str(response.status_code),
        )
        REQ_COUNT.labels(*labels).inc()
        REQ_LATENCY.labels(*labels).observe(
            time.perf_counter()
            - request.scope.get("_empyrean_metrics_start", time.perf_counter())
        )
        return response

    @app.route("/metrics")
    async def metrics():
        # H19: when METRICS_SECRET is configured, require a matching
        # X-Metrics-Secret header so the endpoint is safe even if the API is
        # ever exposed without the nginx 127.0.0.1 allowlist in front of it.
        from config import get_config

        secret = get_config().METRICS_SECRET
        if secret:
            provided = request.headers.get("X-Metrics-Secret", "")
            if provided != secret:
                return "Forbidden", 403
        # A 2-tuple (body, content_type) would be parsed as a status code.
        return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}
