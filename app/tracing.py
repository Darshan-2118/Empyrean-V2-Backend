from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.celery import CeleryInstrumentor
import os

# Default to console output in development
OTLP_ENDPOINT = os.getenv('OTLP_ENDPOINT')
if not OTLP_ENDPOINT:
    # Add console exporter for development
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter
    span_exporter = BatchSpanProcessor(ConsoleSpanExporter())
else:
    # For production, use OTLP exporter
    span_exporter = BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT))

tracer_provider = TracerProvider(resource=Resource.create({'service.name': 'empyrean-backend'}))
tracer_provider.add_span_processor(span_exporter)

trace.set_tracer_provider(tracer_provider)

# Instrument FastAPI (will be called from app factory)
def instrument_app(app):
    FastAPIInstrumentor().instrument_app(app)
    CeleryInstrumentor().instrument()

# Get tracer for manual tracing
def get_tracer(name: str = __name__):
    return trace.get_tracer(name)