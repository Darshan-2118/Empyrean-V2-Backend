import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.celery import CeleryInstrumentor


_instrumented = False
tracer_provider = None
_tracing_shutdown = False  # L59: makes shutdown_tracing idempotent

_log = logging.getLogger("empyrean.tracing")


def shutdown_tracing():
    """Flush and shut down the active tracer provider, stopping its workers.

    The ``BatchSpanProcessor`` exports spans from a background thread holding
    the current stdout. Under pytest, that stream is closed when the session
    ends — if our thread outlives it, the exporter raises
    ``ValueError: I/O operation on closed file`` after the test summary.
    Calling this at process/fixture teardown flushes pending spans while the
    stream is still open, then stops the thread cleanly.

    L59: idempotent — registered as a Quart after_serving hook and also
    called from test teardown, so it may run more than once per process.
    """
    global _tracing_shutdown
    if _tracing_shutdown:
        return
    try:
        provider = trace.get_tracer_provider()
        if provider is not None and hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:  # noqa: BLE001 - shutdown must never raise at teardown
        pass
    _tracing_shutdown = True


def instrument_app(app):
    """
    Wire up OpenTelemetry tracing for the Quart app + Celery.

    Safe to call for every created app: the tracer provider and Celery
    instrumentation are configured once per process (guarded by
    ``_instrumented``), while the ASGI middleware is applied to each new
    Quart instance since every app needs its own wrap.

    Span export is only enabled when ``OTLP_ENDPOINT`` is set. With no
    endpoint, the ASGI/Celery instrumentors are still installed (so span
    context exists) but no span processor is attached — this prevents the
    default ``ConsoleSpanExporter`` from dumping every span as a multi-line
    JSON blob to stdout on every request, which previously flooded the
    empyrean-server logs.
    """
    global _instrumented, tracer_provider

    if not _instrumented:
        otlp_endpoint = os.getenv("OTLP_ENDPOINT")

        if otlp_endpoint:
            tracer_provider = TracerProvider(
                resource=Resource.create(
                    {"service.name": "empyrean-backend"}
                )
            )
            tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
            )
            trace.set_tracer_provider(tracer_provider)
            _log.info("OpenTelemetry tracing enabled -> %s", otlp_endpoint)
        else:
            _log.info(
                "OpenTelemetry tracing disabled (OTLP_ENDPOINT not set); "
                "spans will not be exported"
            )

        # Instrument Celery (idempotent: the base instrumentor guards itself,
        # but the flag keeps this branch to once per process regardless).
        CeleryInstrumentor().instrument()
        _instrumented = True

    # Quart is ASGI-based, so use ASGI middleware — per app instance.
    app.asgi_app = OpenTelemetryMiddleware(app.asgi_app)

    # L59: flush the BatchSpanProcessor on graceful shutdown (SIGTERM) —
    # register teardown once per app instance, only when span export is on.
    if tracer_provider is not None and not getattr(app, "_tracing_shutdown_hook", False):
        app.after_serving(shutdown_tracing)
        app._tracing_shutdown_hook = True


def get_tracer(name: str = __name__):
    return trace.get_tracer(name)
