import os

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.celery import CeleryInstrumentor


_instrumented = False
tracer_provider = None


def shutdown_tracing():
    """Flush and shut down the active tracer provider, stopping its workers.

    The ``BatchSpanProcessor`` exports spans from a background thread holding
    the current stdout. Under pytest, that stream is closed when the session
    ends — if our thread outlives it, the exporter raises
    ``ValueError: I/O operation on closed file`` after the test summary.
    Calling this at process/fixture teardown flushes pending spans while the
    stream is still open, then stops the thread cleanly.

    Resolved through the OTel API rather than this module's ``tracer_provider``
    global: ``app_factory/factory.py`` loads this file as a fresh module
    instance per app creation, so the global may not reference the live
    provider.
    """
    try:
        provider = trace.get_tracer_provider()
        if provider is not None and hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:  # noqa: BLE001 - shutdown must never raise at teardown
        pass


def instrument_app(app):
    """
    Wire up OpenTelemetry tracing for the Quart app + Celery.

    Safe to call more than once: the tracer provider is only configured
    the first time, and ASGI/Celery instrumentation is only applied once
    per process.
    """
    global _instrumented

    if _instrumented:
        return

    otlp_endpoint = os.getenv("OTLP_ENDPOINT")

    if not otlp_endpoint:
        # Default to console output in development
        span_processor = BatchSpanProcessor(
            ConsoleSpanExporter()
        )
    else:
        # For production, use OTLP exporter
        span_processor = BatchSpanProcessor(
            OTLPSpanExporter(endpoint=otlp_endpoint)
        )

    tracer_provider = TracerProvider(
        resource=Resource.create(
            {"service.name": "empyrean-backend"}
        )
    )

    tracer_provider.add_span_processor(span_processor)

    trace.set_tracer_provider(tracer_provider)

    # Quart is ASGI-based, so use ASGI middleware
    app.asgi_app = OpenTelemetryMiddleware(app.asgi_app)

    # Instrument Celery
    CeleryInstrumentor().instrument()

    _instrumented = True


def get_tracer(name: str = __name__):
    return trace.get_tracer(name)