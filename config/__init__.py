"""Application configuration via pydantic-settings.

All values are read from ``.env`` automatically, with sensible defaults.
Type coercion (int, bool, etc.) is handled by pydantic.

``get_config()`` builds the config once and caches it (N-8) so importing
modules don't re-parse ``.env`` on every call. Tests/scripts that repoint env
vars after the first build call ``reset_config_cache()`` to drop the cached
instance.
"""

from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    APP_ENV: str = "development"
    SECRET_KEY: str = "dev-secret-key"
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql://user:pass@localhost:5432/airquality"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # MQTT
    MQTT_BROKER_HOST: str = "localhost"
    MQTT_BROKER_PORT: int = 1883
    MQTT_USE_TLS: bool = False
    MQTT_TLS_CERT: str = ""
    MQTT_TLS_KEY: str = ""
    MQTT_CA_CERTS: str = ""

    # JWT
    JWT_SECRET: str = "dev-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRY_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRY_DAYS: int = 7

    # AQI Thresholds
    AQI_WARNING_THRESHOLD: int = 100
    AQI_CRITICAL_THRESHOLD: int = 150

    # Data Retention
    DATA_RETENTION_DAYS: int = 365

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

    @model_validator(mode="after")
    def _reject_dev_secrets_in_production(self) -> "Config":
        """Fail fast in production if a secret is weak or a dev placeholder.

        A publicly-known / empty / single-char JWT_SECRET would let anyone mint
        tokens, so any weak value turns this into a startup error (H-7). The
        blocklist is kept in sync with .env.example.
        """
        if self.APP_ENV == "production":
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
                    "Refusing to start in production: "
                    f"{', '.join(bad)} is missing, too short "
                    f"(<{_MIN_SECRET_BYTES} bytes), low-entropy, or still a "
                    "development default. Set real secrets in .env."
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
