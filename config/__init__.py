"""Application configuration via pydantic-settings.

All values are read from ``.env`` automatically, with sensible defaults.
Type coercion (int, bool, etc.) is handled by pydantic.

``get_config()`` builds the config once and caches it (N-8) so importing
modules don't re-parse ``.env`` on every call. Tests/scripts that repoint env
vars after the first build call ``reset_config_cache()`` to drop the cached
instance.
"""

import logging
import logging
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Known development placeholder secrets — refuse to start with these in prod.
# Kept in sync with .env.example so the shipped placeholders (both the
# SECRET_KEY ``change-me-to-a-random-secret`` and the JWT ``change-me-to-a-256-
# bit-random-secret``) are always blocked (H-7).
_DEV_SECRETS = {
    "dev-secret-key",
    "dev-jwt-secret",
    "change-me-to-a-random-secret",
    "change-me-to-a-256-bit-random-secret",
}

# Minimum acceptable secret strength in production (H-7).
_MIN_SECRET_BYTES = 32
# A secret made of fewer than 5 distinct characters (e.g. ``"a" * 32`` or
# ``"1234" * 8``) has trivial entropy even at length — reject it too.
_MIN_SECRET_DISTINCT_CHARS = 5


def _is_weak_secret(value: str) -> bool:
    """True when *value* is a known placeholder or too weak for production."""
    if value in _DEV_SECRETS:
        return True
    if len(value.encode("utf-8")) < _MIN_SECRET_BYTES:
        return True
    return len(set(value)) < _MIN_SECRET_DISTINCT_CHARS


class Config(BaseSettings):
    """Configuration loaded from environment / .env file."""

    # Resolve .env relative to the repo root (this file), so scripts work
    # from any working directory instead of silently falling back to defaults.
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
    )

    # App
    # Ensure APP_ENV validation - "development" is explicit; any other value
    # should be treated strictly as production for secret validation purposes.
    # Users must set APP_ENV explicitly in production.
    APP_ENV: str = "development"
    SECRET_KEY: str = "dev-secret-key"
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql://user:pass@localhost:5432/airquality"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # MQTT
    MQTT_ENABLED: bool = False
    MQTT_BROKER_HOST: str = "localhost"
    MQTT_BROKER_PORT: int = 1883
    MQTT_USE_TLS: bool = True  # #20: default to TLS for production-readiness
    MQTT_TLS_CERT: str = ""
    MQTT_TLS_KEY: str = ""
    MQTT_CA_CERTS: str = ""

    # JWT
    JWT_SECRET: str = "dev-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRY_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRY_DAYS: int = 7

    # SMTP (fail-soft alert email, see tasks/alerts.py). All empty by default so
    # email alerts are a no-op unless explicitly configured — never raises.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_USE_TLS: bool = True

    # AQI Thresholds
    AQI_WARNING_THRESHOLD: int = 100
    AQI_CRITICAL_THRESHOLD: int = 150

    # Data Retention
    DATA_RETENTION_DAYS: int = 365

    # Task Timeouts
    TASK_SOFT_TIME_LIMIT: int = 300  # Seconds before soft kill of Celery tasks
    TASK_HARD_TIME_LIMIT: int = 600  # Seconds before hard kill of Celery tasks
    TASK_TIMEOUT_ENV_WARNING: int = 600  # Env var override hours (warn if exceeded during unit tests)

    # MQTT Queue
    MQTT_QUEUE_MAX: int = 1000  # Maximum pending messages in worker queue
    MQTT_ENQUEUE_MAX_ATTEMPTS: int = 5  # Max retry attempts for blocked queue enqueue
    MQTT_ENQUEUE_TIMEOUT: float = 0.5  # Seconds to wait for queue space

    # CORS — comma-separated origins stored as a string, accessed as a list.
    # The default covers both dev frontends so neither loses CORS headers;
    # keep in sync with docs/api.md and .env.example (L-32).
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # ── Derived properties ────────────────────────────────────────────────

    @property
    def DEBUG(self) -> bool:
        """Auto-debug in development mode."""
        return self.APP_ENV == "development"

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS origins as a list (split on comma)."""
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # ── Validators ────────────────────────────────────────────────────────

    @field_validator("LOG_LEVEL")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}, got {v!r}")
        return upper

    @field_validator("DATABASE_URL")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        """Reject an obviously-misconfigured DB URL at config load (#17).

        Engines are built lazily and only fail on first query, so a wrong/missing
        URL boots "fine" then 500s every DB route. Validate the shape up front:
        must be a postgres(-variant) scheme with a host present. In production a
        placeholder/empty password is refused (mirrors the secret guard).
        """
        from urllib.parse import urlparse

        parsed = urlparse(v)
        if parsed.scheme not in {"postgresql", "postgres", "postgresql+psycopg", "postgresql+asyncpg"}:
            raise ValueError(
                f"DATABASE_URL must be a postgresql:// URL, got scheme "
                f"{parsed.scheme!r} in {v!r}"
            )
        if not parsed.hostname:
            raise ValueError(f"DATABASE_URL must include a host, got {v!r}")
        # Password check is gated on APP_ENV, read from the raw env value so we
        # don't depend on field ordering within the model.
        app_env = (cls._raw_app_env() or "development").strip().lower()
        if app_env == "production" and not parsed.password:
            raise ValueError(
                "DATABASE_URL has no password in production — refusing to start "
                "with a placeholder/empty DB credential."
            )
        return v

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _validate_cors_origins(cls, v: str) -> str:
        """Reject overly-permissive CORS origins, especially in production (#20).

        Splits the comma-separated value and refuses ``*`` (wildcard, opens the
        API to any website). In production any wildcard is a hard error; in
        other environments it logs a warning so the operator is still told the
        setting is dangerous without blocking local development.
        """
        origins = [o.strip() for o in v.split(",") if o.strip()]
        app_env = (cls._raw_app_env() or "development").strip().lower()
        if "*" in origins:
            if app_env == "production":
                raise ValueError(
                    "CORS_ORIGINS must not contain '*' in production — set "
                    "explicit origins (e.g. https://app.example.com)"
                )
            logger.warning(
                "CORS_ORIGINS contains '*' which allows any website to call "
                "the API — restrict it before deploying to production"
            )
        return v

    @classmethod
    def _raw_app_env(cls) -> str | None:
        """Best-effort read of APP_ENV from env/.env for cross-field validation.

        ``urlparse`` has already run, so ``_validate_database_url`` needs the
        *production* decision without relying on field declaration order.
        """
        import os

        from pathlib import Path

        env_val = os.environ.get("APP_ENV")
        if env_val is not None:
            return env_val
        dotenv = Path(__file__).resolve().parents[1] / ".env"
        if dotenv.exists():
            for line in dotenv.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("APP_ENV") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        return None

    @model_validator(mode="after")
    def _validate_aqi_thresholds(self) -> "Config":
        """Validate AQI thresholds are in logical order (#43).

        Warning threshold must be lower than critical threshold, otherwise alerts
        may be triggered incorrectly or skipped entirely.
        """
        if self.AQI_WARNING_THRESHOLD >= self.AQI_CRITICAL_THRESHOLD:
            raise ValueError(
                "AQI_WARNING_THRESHOLD must be less than AQI_CRITICAL_THRESHOLD"
            )
        return self

    @model_validator(mode="after")
    def _reject_dev_secrets_in_all_environments(self) -> "Config":
        """Fail fast in all environments if a secret is weak or a dev placeholder.

        Known placeholders and weak secrets must never be accepted, even in
        development. This prevents production deployments from accidentally
        running with insecure defaults (e.g. when APP_ENV is unset or misspelled).
        The blocklist is kept in sync with .env.example.
        """
        # Always reject known weak secrets, regardless of APP_ENV
        # This prevents silent security failures when APP_ENV is misspelled or unset
        bad = [
            name
            for name, value in (
                ("SECRET_KEY", self.SECRET_KEY),
                ("JWT_SECRET", self.JWT_SECRET),
            )
            if _is_weak_secret(value)
        ]
        if bad:
            raise ValueError(
                "Refusing to start with weak secrets: "
                f"{', '.join(bad)} is missing, too short "
                f"(<{_MIN_SECRET_BYTES} bytes), low-entropy, or still a "
                "development default. Set real secrets in .env."
            )
        return self

    @model_validator(mode="after")
    def _validate_task_time_limits(self) -> "Config":
        """Validate task time limits are in logical order (#43).

        Soft time limit must be less than hard time limit to allow for graceful
        shutdown before hard termination.
        """
        if self.TASK_SOFT_TIME_LIMIT >= self.TASK_HARD_TIME_LIMIT:
            raise ValueError(
                "TASK_SOFT_TIME_LIMIT must be less than TASK_HARD_TIME_LIMIT"
            )
        return self

    @model_validator(mode="after")
    def _validate_mqtt_tls_settings(self) -> "Config":
        """Validate MQTT TLS settings consistency (#43).

        If TLS is enabled, certificate and key files should be provided.
        """
        if self.MQTT_ENABLED and self.MQTT_USE_TLS and not (self.MQTT_TLS_CERT and self.MQTT_TLS_KEY):
            raise ValueError(
                "MQTT_TLS_CERT and MQTT_TLS_KEY must be set when MQTT_USE_TLS is True"
            )
        return self

    @model_validator(mode="after")
    def _validate_redis_url(self) -> "Config":
        """Validate Redis URL format (#43).

        Ensures Redis URL uses the redis:// scheme.
        """
        if not self.REDIS_URL.startswith("redis://"):
            raise ValueError(
                "REDIS_URL must be a redis:// URL"
            )
        return self


# Cached config instance so importing modules don't re-parse .env on every
# get_config() call (N-8). ``reset_config_cache`` exists for tests/scripts that
# repoint env vars after the first build.
_config_cache: Config | None = None


def get_config() -> Config:
    """Return the application configuration, built once and cached (N-8)."""
    global _config_cache
    if _config_cache is None:
        _config_cache = Config()
    return _config_cache


def reset_config_cache() -> None:
    """Drop the cached Config so the next ``get_config()`` re-reads env/.env."""
    global _config_cache
    _config_cache = None
