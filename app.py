import logging
import sys

from quart import Quart, jsonify
from quart_cors import cors

from config import get_config
from models.base import dispose_engines


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
    app = Quart(__name__)
    app.config.from_object(cfg)
    app.secret_key = cfg.SECRET_KEY

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

    # --- CORS ---
    _app = cors(app, allow_origin=cfg.cors_origins_list, allow_credentials=True)

    # --- Error handlers (RFC 7807 problem+json) ---
    def _problem_json(status: int, title: str, detail: str | None = None):
        """Factory: return an RFC 7807 error handler."""
        async def handler(e):
            return jsonify({
                "type": "about:blank",
                "title": title,
                "status": status,
                "detail": detail or str(e),
            }), status, {"Content-Type": "application/problem+json"}
        return handler

    _app.errorhandler(400)(_problem_json(400, "Bad Request"))
    _app.errorhandler(401)(_problem_json(401, "Unauthorized", "Authentication is required"))
    _app.errorhandler(403)(_problem_json(403, "Forbidden", "You do not have permission to access this resource"))
    _app.errorhandler(404)(_problem_json(404, "Not Found", "The requested resource was not found"))
    _app.errorhandler(422)(_problem_json(422, "Unprocessable Entity"))
    _app.errorhandler(429)(_problem_json(429, "Too Many Requests", "Rate limit exceeded. Please slow down."))

    @_app.errorhandler(500)
    async def internal_error(e):
        logger.exception("Internal server error")
        return jsonify({
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            "detail": "An unexpected error occurred",
        }), 500, {"Content-Type": "application/problem+json"}

    # --- Health endpoint ---
    @_app.route("/health")
    async def health():
        return jsonify({"status": "ok", "environment": cfg.APP_ENV})

    # --- Blueprint registration (incremental — more added in later phases) ---
    from api.auth import auth_bp
    from api.profile import profile_bp
    # from api.readings import readings_bp
    # from api.nodes import nodes_bp
    # from api.alerts import alerts_bp
    # from api.forecast import forecast_bp
    # from api.export import export_bp
    # from api.admin import admin_bp

    _app.register_blueprint(auth_bp, url_prefix="/api/v1/auth")
    _app.register_blueprint(profile_bp, url_prefix="/api/v1/profile")
    # _app.register_blueprint(readings_bp, url_prefix="/api/v1/readings")
    # _app.register_blueprint(nodes_bp, url_prefix="/api/v1/nodes")
    # _app.register_blueprint(alerts_bp, url_prefix="/api/v1/alerts")
    # _app.register_blueprint(forecast_bp, url_prefix="/api/v1/forecast")
    # _app.register_blueprint(export_bp, url_prefix="/api/v1/export")
    # _app.register_blueprint(admin_bp, url_prefix="/api/v1/admin")

    return _app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=8000)
