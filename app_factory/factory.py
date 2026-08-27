import logging

from quart import Quart
from quart_cors import cors

from config import get_config
from .logging_setup import setup_logging

from .helpers import (
    register_blueprints,
    register_database_lifecycle,
    register_error_handlers,
    register_health,
    register_mqtt_lifecycle,
    register_redis_lifecycle,
    register_request_middleware,
    register_startup_checks,
)

def create_app() -> Quart:
    """Application factory."""
    cfg = get_config()

    # M7: one-shot logging — idempotent across repeated create_app() calls and
    # worker processes, so no duplicate root handlers accumulate.
    setup_logging(level=getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO))
    logger = logging.getLogger("empyrean")
    logger.info("Starting Empyrean backend — environment: %s", cfg.APP_ENV)

    app = Quart(__name__, static_folder=None)
    app.config.from_object(cfg)
    app.secret_key = cfg.SECRET_KEY
    # M5: request-body cap promoted to config (MAX_CONTENT_LENGTH, bytes).
    # Sized for CSV node-config uploads; bulk readings ingest goes over MQTT,
    # not HTTP, so it is not bounded by this.
    app.config["MAX_CONTENT_LENGTH"] = cfg.MAX_CONTENT_LENGTH

    register_database_lifecycle(app, logger)
    register_redis_lifecycle(app)
    register_startup_checks(app, cfg, logger)
    register_mqtt_lifecycle(app, cfg, logger)

    # Setup distributed tracing (M4): a normal package import. Tracing lives
    # in app_factory/, so there is no root-module collision to hack around.
    try:
        from .tracing import instrument_app

        instrument_app(app)
    except Exception as trace_exc:
        logger.warning("OpenTelemetry tracing unavailable (fail-open): %s", trace_exc)

    wrapped_app = cors(app, allow_origin=cfg.cors_origins_list, allow_credentials=True)

    register_error_handlers(wrapped_app, logger)
    register_request_middleware(wrapped_app)
    from api.metrics import register_metrics

    register_metrics(wrapped_app)
    register_health(wrapped_app, cfg, logger)
    register_blueprints(wrapped_app)

    return wrapped_app
