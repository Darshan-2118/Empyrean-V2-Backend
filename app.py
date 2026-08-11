import logging
import os
import sys

from quart import Quart, jsonify
from quart_cors import cors

from api.jwt import _problem_json
from config import get_config
from models.base import dispose_engines

# The running MQTT ingestion client, if started (M-10). Module-level so the
# before/after_serving hooks can share it across the app's lifetime.
_mqtt_client = None


def create_app() -> Quart:
    """Application factory."""
    cfg = get_config()

    # --- Logging setup ---
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)-16s  %(message)s",
        stream=sys.stdout,
    )
    logger = logging.getLogger("empyrean")
    logger.info("Starting Empyrean backend — environment: %s", cfg.APP_ENV)

    # --- Create app ---
    # static_folder=None so the default /static/<path:filename> route is not
    # registered — otherwise a static/ dir created later would be served with
    # no auth (L-34).
    app = Quart(__name__, static_folder=None)
    app.config.from_object(cfg)
    app.secret_key = cfg.SECRET_KEY

    # M-13: hard cap on request-body size. Without this a multi-hundred-MB JSON
    # body to login/refresh/logout is fully parsed into memory — an easy
    # memory-exhaustion DoS. 64 KB comfortably fits every endpoint's payload.
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64 KB

    # --- DB lifecycle hooks ---
    @app.before_serving
    async def check_db():
        """Verify the database is reachable before serving requests."""
        from sqlalchemy import text as sa_text
        from models.base import async_engine

        async with async_engine.connect() as conn:
            await conn.execute(sa_text("SELECT 1"))
        logger.info("Database connection verified.")

    @app.after_serving
    async def shutdown_db():
        """Dispose connection pools on app shutdown."""
        await dispose_engines()

    # --- MQTT lifecycle (M-10) ---
    # Wire the ingestion client into the app process so the documented
    # deployment shape actually runs it. Gated behind the ``MQTT_ENABLED`` env
    # var so deployments without MQTT are unaffected, and made fail-soft: if
    # paho isn't installed, the broker is unreachable, TLS is misconfigured, or
    # start() raises for any reason, we log and keep serving HTTP — a bad broker
    # must not take the API down.

    @app.before_serving
    async def start_mqtt():
        """Start the MQTT ingestion client, tolerating an unavailable setup."""
        global _mqtt_client
        if os.environ.get("MQTT_ENABLED", "0").lower() not in {"1", "true", "yes"}:
            logger.info("MQTT_ENABLED unset — MQTT ingestion client not started")
            return
        try:
            # Import lazily so a missing paho-mqtt (or mqtt package) is a
            # logged warning, not an app-level import error.
            from mqtt.client import MQTTClient

            client = MQTTClient()
        except Exception:
            logger.exception(
                "MQTT client unavailable — continuing without MQTT ingestion"
            )
            return
        try:
            client.start()
        except Exception:
            logger.exception(
                "MQTT client failed to start — continuing without MQTT ingestion"
            )
            return
        _mqtt_client = client
        from mqtt.registry import set_client; set_client(client)
        logger.info("MQTT ingestion client started (M-10 lifecycle wiring)")

    @app.after_serving
    async def stop_mqtt():
        """Stop the MQTT ingestion client if it was started."""
        global _mqtt_client
        if _mqtt_client is None:
            return
        try:
            _mqtt_client.stop()
        except Exception:
            logger.exception("MQTT client failed to stop cleanly")
        _mqtt_client = None
        from mqtt.registry import set_client; set_client(None)

    # --- CORS ---
    _app = cors(app, allow_origin=cfg.cors_origins_list, allow_credentials=True)

    # --- Error handlers (RFC 7807 problem+json) ---
    @_app.errorhandler(400)
    async def bad_request(e):
        return _problem_json(400, "Bad Request")

    @_app.errorhandler(401)
    async def unauthorized(e):
        return _problem_json(401, "Unauthorized", "Authentication is required")

    @_app.errorhandler(403)
    async def forbidden(e):
        return _problem_json(
            403, "Forbidden", "You do not have permission to access this resource"
        )

    @_app.errorhandler(404)
    async def not_found(e):
        return _problem_json(404, "Not Found", "The requested resource was not found")

    @_app.errorhandler(405)
    async def method_not_allowed(e):
        return _problem_json(405, "Method Not Allowed")

    @_app.errorhandler(422)
    async def unprocessable_entity(e):
        return _problem_json(422, "Unprocessable Entity")

    @_app.errorhandler(413)
    async def request_entity_too_large(e):
        return _problem_json(
            413, "Request Entity Too Large", "Request body exceeds 64 KB"
        )

    @_app.errorhandler(429)
    async def too_many_requests(e):
        return _problem_json(
            429, "Too Many Requests", "Rate limit exceeded. Please slow down."
        )

    @_app.errorhandler(500)
    async def internal_error(e):
        logger.exception("Internal server error")
        return _problem_json(
            500, "Internal Server Error", "An unexpected error occurred"
        )

    # --- Request logging middleware (Phase 12) ---
    # One INFO line per HTTP request on the dedicated ``empyrean.request``
    # logger — method, path (no query string), status, duration_ms. Registered
    # on _app (the cors-wrapped app) alongside the error handlers; WebSocket
    # handshakes are not HTTP requests and are never logged.
    from api.request_log import register_request_logging

    register_request_logging(_app)

    # --- Health endpoint ---
    @_app.route("/health")
    async def health():
        return jsonify({"status": "ok", "environment": cfg.APP_ENV})

    # --- Blueprint registration (incremental — more added in later phases) ---
    from api.auth import auth_bp
    from api.forecast import forecast_bp
    from api.profile import profile_bp
    from api.readings import readings_bp
    from api.nodes import nodes_bp
    from api.alerts import alerts_bp
    from api.ws.routes import ws_bp
    from api.admin import admin_bp
    from api.export import export_bp

    _app.register_blueprint(auth_bp, url_prefix="/api/v1/auth")
    _app.register_blueprint(profile_bp, url_prefix="/api/v1/profile")
    _app.register_blueprint(readings_bp, url_prefix="/api/v1/readings")
    _app.register_blueprint(forecast_bp, url_prefix="/api/v1/forecast")
    _app.register_blueprint(nodes_bp, url_prefix="/api/v1/nodes")
    _app.register_blueprint(alerts_bp, url_prefix="/api/v1/alerts")
    _app.register_blueprint(ws_bp, url_prefix="/ws")
    _app.register_blueprint(admin_bp, url_prefix="/api/v1/admin")
    _app.register_blueprint(export_bp, url_prefix="/api/v1/export")

    return _app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=8000)
