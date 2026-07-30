"""Application configuration via pydantic-settings.

All values are read from ``.env`` automatically, with sensible defaults.
Type coercion (int, bool, etc.) is handled by pydantic.
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Configuration loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

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

    # CORS — comma-separated origins stored as a string, accessed as a list
    CORS_ORIGINS: str = "http://localhost:3000"

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


class DevelopmentConfig(Config):
    """Development — debug defaults."""
    APP_ENV: str = "development"


class ProductionConfig(Config):
    """Production — debug is always off."""
    APP_ENV: str = "production"


_config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
}


def get_config() -> Config:
    """Return the right config class for the current environment."""
    env = Config().APP_ENV  # fresh load to get the actual env
    cls = _config_map.get(env, DevelopmentConfig)
    return cls()
