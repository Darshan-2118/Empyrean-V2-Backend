import time
import re
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from quart import request

# Sanitize label values to be valid Prometheus label characters and within length limits.
# Keeps cardinality under control by collapsing dynamic parts (e.g., variables) into a safe token.
def _sanitize(label: str) -> str:
    # Replace anything that isn't alphanumeric or underscore with an underscore
    label = re.sub(r'[^a-zA-Z0-9_]', '_', label)
    # Collapse repeated underscores
    label = re.sub(r'_+', '_', label)
    # Trim to a reasonable length (Prometheus max 63 chars per label value)
    return label[:63].strip('_')


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
        raw_route = str(request.url_rule or "unknown")
        sanitized_route = _sanitize(raw_route)

        labels = (
            request.method,
            sanitized_route,
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
        # A 2-tuple (body, content_type) would be parsed as a status code.
        return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}
