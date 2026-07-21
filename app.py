import logging
import sys

from quart import Quart, jsonify
from quart_cors import cors

from config import get_config


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

    # --- CORS ---
    _app = cors(app, allow_origin=cfg.CORS_ORIGINS, allow_credentials=True)

    # --- Error handlers ---
    @_app.errorhandler(400)
    async def bad_request(e):
        return jsonify({
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": str(e),
        }), 400, {"Content-Type": "application/problem+json"}

    @_app.errorhandler(401)
    async def unauthorized(e):
        return jsonify({
            "type": "about:blank",
            "title": "Unauthorized",
            "status": 401,
            "detail": "Authentication is required",
        }), 401, {"Content-Type": "application/problem+json"}

    @_app.errorhandler(403)
    async def forbidden(e):
        return jsonify({
            "type": "about:blank",
            "title": "Forbidden",
            "status": 403,
            "detail": "You do not have permission to access this resource",
        }), 403, {"Content-Type": "application/problem+json"}

    @_app.errorhandler(404)
    async def not_found(e):
        return jsonify({
            "type": "about:blank",
            "title": "Not Found",
            "status": 404,
            "detail": "The requested resource was not found",
        }), 404, {"Content-Type": "application/problem+json"}

    @_app.errorhandler(422)
    async def unprocessable(e):
        return jsonify({
            "type": "about:blank",
            "title": "Unprocessable Entity",
            "status": 422,
            "detail": str(e),
        }), 422, {"Content-Type": "application/problem+json"}

    @_app.errorhandler(429)
    async def too_many_requests(e):
        return jsonify({
            "type": "about:blank",
            "title": "Too Many Requests",
            "status": 429,
            "detail": "Rate limit exceeded. Please slow down.",
        }), 429, {"Content-Type": "application/problem+json"}

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

    # --- Blueprint registration (to be filled in later phases) ---
    # from api.auth import auth_bp
    # from api.readings import readings_bp
    # from api.nodes import nodes_bp
    # from api.alerts import alerts_bp
    # from api.forecast import forecast_bp
    # from api.export import export_bp
    # from api.profile import profile_bp
    # from api.admin import admin_bp

    # _app.register_blueprint(auth_bp, url_prefix="/api/v1/auth")
    # _app.register_blueprint(readings_bp, url_prefix="/api/v1/readings")
    # _app.register_blueprint(nodes_bp, url_prefix="/api/v1/nodes")
    # _app.register_blueprint(alerts_bp, url_prefix="/api/v1/alerts")
    # _app.register_blueprint(forecast_bp, url_prefix="/api/v1/forecast")
    # _app.register_blueprint(export_bp, url_prefix="/api/v1/export")
    # _app.register_blueprint(profile_bp, url_prefix="/api/v1/profile")
    # _app.register_blueprint(admin_bp, url_prefix="/api/v1/admin")

    return _app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=8000)
