"""
Helper functions used by app_factory.factory.create_app().

Each register_* function takes the Quart `app` instance (or CORS-wrapped app)
and attaches the relevant behavior, lifecycle hooks, middleware, error handlers,
and blueprints.
"""

from __future__ import annotations

import logging
from typing import Any

from quart import Quart, g, jsonify, request
from werkzeug.exceptions import HTTPException

from api.jwt import _problem_json
from api.request_log import register_request_logging
from models.base import AsyncSessionLocal, dispose_engines

logger = logging.getLogger("empyrean.app")


def register_request_middleware(app: Quart) -> None:
    """Bind request logging and per-request DB session factory to `g`."""
    register_request_logging(app)

    @app.before_request
    async def bind_request_session():
        if not hasattr(g, "request_session_factory"):
            g.request_session_factory = AsyncSessionLocal

    @app.after_request
    async def teardown_request(response: Any) -> Any:
        return response


def get_request_session():
    """Return the per-request session factory bound in `g` by bind_request_session()."""
    return getattr(g, "request_session_factory", AsyncSessionLocal)


def register_database_lifecycle(app: Quart, cfg, app_logger: logging.Logger) -> None:
    """Wire up database engine disposal on application shutdown."""
    @app.after_serving
    async def shutdown_db():
        app_logger.info("Closing database engine connections...")
        await dispose_engines()


def register_redis_lifecycle(app: Quart, cfg, app_logger: logging.Logger) -> None:
    """Wire up Redis client lifecycle."""
    @app.after_serving
    async def shutdown_redis():
        from api.cache import get_client as get_cache_client
        from api.rate_limit import get_client as get_rl_client

        for getter in (get_cache_client, get_rl_client):
            try:
                client = getter()
                if client is not None:
                    await client.close()
            except Exception:
                pass


def register_startup_checks(app: Quart, cfg, app_logger: logging.Logger) -> None:
    """Run pre-flight logging on startup."""
    @app.before_serving
    async def startup():
        app_logger.info("Application startup complete (env=%s)", cfg.APP_ENV)


def register_mqtt_lifecycle(app: Quart, cfg, app_logger: logging.Logger) -> None:
    """Wire up MQTT client connect on startup and disconnect on shutdown."""
    @app.before_serving
    async def start_mqtt():
        if cfg.MQTT_ENABLED:
            try:
                from mqtt.client import MQTTClient
                from mqtt.registry import set_client

                client = MQTTClient()
                client.start()
                set_client(client)
                app_logger.info("MQTT client started and registered")
            except Exception as exc:
                app_logger.warning("MQTT client failed to start (fail-open): %s", exc)

    @app.after_serving
    async def stop_mqtt():
        from mqtt.registry import get_client, set_client

        client = get_client()
        if client is not None:
            try:
                client.stop()
            except Exception:
                pass
            set_client(None)


def register_error_handlers(app: Quart, app_logger: logging.Logger) -> None:
    """Register RFC 7807 problem+json error handlers."""
    @app.errorhandler(400)
    async def bad_request(error):
        detail = getattr(error, "description", "Request body is required or malformed")
        return _problem_json(400, "Bad Request", detail)

    @app.errorhandler(401)
    async def unauthorized(error):
        detail = getattr(error, "description", "Unauthorized")
        return _problem_json(401, "Unauthorized", detail)

    @app.errorhandler(403)
    async def forbidden(error):
        detail = getattr(error, "description", "Forbidden")
        return _problem_json(403, "Forbidden", detail)

    @app.errorhandler(404)
    async def not_found(error):
        detail = getattr(error, "description", "Resource not found")
        return _problem_json(404, "Not Found", detail)

    @app.errorhandler(405)
    async def method_not_allowed(error):
        detail = getattr(error, "description", "Method not allowed")
        return _problem_json(405, "Method Not Allowed", detail)

    @app.errorhandler(422)
    async def unprocessable_entity(error):
        detail = getattr(error, "description", "Unprocessable Entity")
        return _problem_json(422, "Unprocessable Entity", detail)

    @app.errorhandler(429)
    async def rate_limited(error):
        detail = getattr(error, "description", "Rate limit exceeded")
        return _problem_json(429, "Too Many Requests", detail)

    @app.errorhandler(HTTPException)
    async def http_exception_handler(error: HTTPException):
        code = error.code or 500
        title = error.name or "Error"
        detail = error.description or title
        return _problem_json(code, title, detail)

    @app.errorhandler(Exception)
    async def unhandled_exception(error: Exception):
        app_logger.exception("Unhandled application error: %s", error)
        return _problem_json(500, "Internal Server Error", "An unexpected error occurred")


def register_health(app: Quart, cfg, app_logger: logging.Logger) -> None:
    """Register the unauthenticated /health liveness endpoint."""
    @app.route("/health", methods=["GET"])
    async def health_check():
        return jsonify({"status": "ok", "environment": cfg.APP_ENV}), 200


def register_blueprints(app: Quart) -> None:
    """Register all Quart blueprints with documented API routes."""
    from api.admin import admin_bp
    from api.alerts import alerts_bp
    from api.auth import auth_bp
    from api.export import export_bp
    from api.forecast import forecast_bp
    from api.nodes import nodes_bp
    from api.profile import profile_bp
    from api.readings import readings_bp
    from api.ws.routes import ws_bp

    app.register_blueprint(auth_bp, url_prefix="/api/v1/auth")
    app.register_blueprint(profile_bp, url_prefix="/api/v1/profile")
    app.register_blueprint(readings_bp, url_prefix="/api/v1/readings")
    app.register_blueprint(forecast_bp, url_prefix="/api/v1/forecast")
    app.register_blueprint(nodes_bp, url_prefix="/api/v1/nodes")
    app.register_blueprint(alerts_bp, url_prefix="/api/v1/alerts")
    app.register_blueprint(admin_bp, url_prefix="/api/v1/admin")
    app.register_blueprint(export_bp, url_prefix="/api/v1/export")
    app.register_blueprint(ws_bp, url_prefix="/ws")