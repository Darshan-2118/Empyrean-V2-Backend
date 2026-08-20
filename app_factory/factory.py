import logging
import sys

from quart import Quart
from quart_cors import cors

from config import get_config

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

# Import tracing setup
from app.tracing import tracer_provider


def create_app() -> Quart:
    """Application factory."""
    cfg = get_config()

    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)-16s  %(message)s",
        stream=sys.stdout,
    )
    logger = logging.getLogger("empyrean")
    logger.info("Starting Empyrean backend — environment: %s", cfg.APP_ENV)

    app = Quart(__name__, static_folder=None)
    app.config.from_object(cfg)
    app.secret_key = cfg.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    register_database_lifecycle(app, cfg, logger)
    register_redis_lifecycle(app, cfg, logger)
    register_startup_checks(app, cfg, logger)
    register_mqtt_lifecycle(app, cfg, logger)

    # Setup distributed tracing
    from app.tracing import instrument_app
    instrument_app(app)

    wrapped_app = cors(app, allow_origin=cfg.cors_origins_list, allow_credentials=True)

    register_error_handlers(wrapped_app, logger)
    register_request_middleware(wrapped_app)
    from api.metrics import register_metrics

    register_metrics(wrapped_app)
    register_health(wrapped_app, cfg, logger)
    register_blueprints(wrapped_app)

    return wrapped_app
