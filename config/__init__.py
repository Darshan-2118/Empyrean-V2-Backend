import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Base configuration."""
    APP_ENV = os.getenv("APP_ENV", "development")
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # Database
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/airquality")

    # Redis
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # MQTT
    MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
    MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "8883"))
    MQTT_TLS_CERT = os.getenv("MQTT_TLS_CERT", "")
    MQTT_TLS_KEY = os.getenv("MQTT_TLS_KEY", "")
    MQTT_CA_CERTS = os.getenv("MQTT_CA_CERTS", "")

    # JWT
    JWT_SECRET = os.getenv("JWT_SECRET", "dev-jwt-secret")
    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_ACCESS_TOKEN_EXPIRY_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRY_MINUTES", "15"))
    JWT_REFRESH_TOKEN_EXPIRY_DAYS = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRY_DAYS", "7"))

    # AQI Thresholds
    AQI_WARNING_THRESHOLD = int(os.getenv("AQI_WARNING_THRESHOLD", "100"))
    AQI_CRITICAL_THRESHOLD = int(os.getenv("AQI_CRITICAL_THRESHOLD", "150"))

    # Data Retention
    DATA_RETENTION_DAYS = int(os.getenv("DATA_RETENTION_DAYS", "365"))

    # CORS
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")


class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG = True


class ProductionConfig(Config):
    """Production configuration."""
    DEBUG = False


config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
}


def get_config() -> Config:
    env = os.getenv("APP_ENV", "development")
    cls = config_map.get(env, DevelopmentConfig)
    return cls()
