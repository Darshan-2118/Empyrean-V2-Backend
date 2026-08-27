"""
Helper functions used by app_factory.factory.create_app().

Each register_* function takes the Quart `app` instance (or CORS-wrapped app)
and attaches the relevant behavior, lifecycle hooks, middleware, error handlers,
and blueprints.
"""

from __future__ import annotations

import logging
from typing import Any

from quart import Quart, jsonify, request
from werkzeug.exceptions import HTTPException

from api.jwt import problem_json
from api.request_log import register_request_logging
from models.base import dispose_engines

logger = logging.getLogger("empyrean.app")

# H2: cap problem+json detail text so Werkzeug/route descriptions can never
# leak long internal strings (validator internals, paths, stack fragments).
_MAX_DETAIL_LEN = 200


def _safe_detail(detail: Any, fallback: str) -> str:
    """Sanitise an error description before it reaches the client (H2).

    Coerces to ``str``, collapses whitespace/newlines, caps length, and falls
    back to the given title when the description is empty.
    """
    if not detail:
        return fallback
    text = " ".join(str(detail).split())
    if len(text) > _MAX_DETAIL_LEN:
        text = text[: _MAX_DETAIL_LEN - 1] + "…"
    return text


def register_request_middleware(app: Quart) -> None:
    """Bind request logging and the CORS-preflight short-circuit to the app.

    L6: the old ``bind_request_session``/``get_request_session`` pair stored a
    session factory on ``g`` that no route ever read — the dead indirection is
    removed; routes open ``AsyncSessionLocal`` sessions directly.
    """
    register_request_logging(app)

    @app.before_request
    async def short_circuit_preflight():
        # H10: CORS preflight (OPTIONS) must not consume a per-route rate-limit
        # slot — an aggressive browser pre-flighting could otherwise lock out
        # its own IP. quart-cors wraps the ASGI app *outside* the Quart app, so
        # it still adds the ACAO headers to this short-circuit response.
        if request.method == "OPTIONS":
            return "", 204


def register_database_lifecycle(app: Quart, app_logger: logging.Logger) -> None:
    """Wire up database engine disposal on application shutdown."""
    @app.after_serving
    async def shutdown_db():
        app_logger.info("Closing database engine connections...")
        await dispose_engines()


def register_redis_lifecycle(app: Quart) -> None:
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
    """Run pre-flight logging and auto-provision the env-configured bootstrap admin."""
    @app.before_serving
    async def startup():
        app_logger.info("Application startup complete (env=%s)", cfg.APP_ENV)
        try:
            from api.auth import ensure_hardcoded_admin
            await ensure_hardcoded_admin()
        except Exception as exc:
            app_logger.warning("Could not ensure bootstrap admin on startup: %s", exc)


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
        detail = _safe_detail(
            getattr(error, "description", None), "Request body is required or malformed"
        )
        return problem_json(400, "Bad Request", detail)

    @app.errorhandler(401)
    async def unauthorized(error):
        detail = _safe_detail(getattr(error, "description", None), "Unauthorized")
        return problem_json(401, "Unauthorized", detail)

    @app.errorhandler(403)
    async def forbidden(error):
        detail = _safe_detail(getattr(error, "description", None), "Forbidden")
        return problem_json(403, "Forbidden", detail)

    @app.errorhandler(404)
    async def not_found(error):
        detail = _safe_detail(getattr(error, "description", None), "Resource not found")
        return problem_json(404, "Not Found", detail)

    @app.errorhandler(405)
    async def method_not_allowed(error):
        detail = _safe_detail(getattr(error, "description", None), "Method not allowed")
        return problem_json(405, "Method Not Allowed", detail)

    @app.errorhandler(422)
    async def unprocessable_entity(error):
        detail = _safe_detail(getattr(error, "description", None), "Unprocessable Entity")
        return problem_json(422, "Unprocessable Entity", detail)

    @app.errorhandler(429)
    async def rate_limited(error):
        detail = _safe_detail(getattr(error, "description", None), "Rate limit exceeded")
        return problem_json(429, "Too Many Requests", detail)

    @app.errorhandler(HTTPException)
    async def http_exception_handler(error: HTTPException):
        code = error.code or 500
        title = error.name or "Error"
        detail = _safe_detail(error.description, title)
        return problem_json(code, title, detail)

    @app.errorhandler(Exception)
    async def unhandled_exception(error: Exception):
        app_logger.exception("Unhandled application error: %s", error)
        return problem_json(500, "Internal Server Error", "An unexpected error occurred")


def register_health(app: Quart, cfg, app_logger: logging.Logger) -> None:
    """Register the unauthenticated /health liveness endpoint.

    H3: the response intentionally carries no environment information — an
    unauthenticated caller must not learn whether a host is production.
    Component-level diagnostics live behind admin auth (GET /api/v1/admin/health).
    """
    @app.route("/health", methods=["GET"])
    async def health_check():
        return jsonify({"status": "ok"}), 200


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