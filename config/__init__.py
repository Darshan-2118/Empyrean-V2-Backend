"""Application configuration via pydantic-settings.

All values are read from ``.env`` automatically, with sensible defaults.
Type coercion (int, bool, etc.) is handled by pydantic.

``get_config()`` builds the config once and caches it (N-8) so importing
modules don't re-parse ``.env`` on every call. Tests/scripts that repoint env
vars after the first build call ``reset_config_cache()`` to drop the cached
instance.
"""

import logging
import os
from pathlib import Path

from pydantic import ValidationInfo, field_validator, model_validator
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


def _is_weak_secret(value: str, *, check_strength: bool = True) -> bool:
    """True when *value* is a known placeholder or too weak for production.

    With ``check_strength=False`` (M67, ``APP_ENV=test`` only) the known
    placeholder blocklist is still enforced but the length/entropy tests are
    skipped.
    """
    if value in _DEV_SECRETS:
        return True
    if not check_strength:
        return False
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

    # Request-body cap in bytes (M5): Quart rejects larger bodies with 413.
    # 64 KB comfortably fits CSV node-config uploads and JSON payloads; bulk
    # sensor ingest goes over MQTT, not HTTP, so it is not bounded by this.
    MAX_CONTENT_LENGTH: int = 64 * 1024

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
    # H36: broker client id. Empty derives a stable per-host id
    # (``empyrean-backend-<hostname>``) so two API *hosts* (or a dev instance
    # against the prod broker) never share one MQTT session — a shared id makes
    # the broker treat every CONNECT as a takeover and drops in-flight QoS 1
    # messages. Keep it stable across restarts so the clean_session=False
    # offline queue keeps working; run only one ingestion client per host.
    MQTT_CLIENT_ID: str = ""

    # JWT
    JWT_SECRET: str = "dev-jwt-secret"
    # H4/M66: the algorithm is pinned to HS256 — this knob is validated below
    # and may never be set to anything else (e.g. "none").
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRY_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRY_DAYS: int = 7

    # L12: maximum accepted password size in UTF-8 bytes, matched to the
    # hashing algorithm's real limit (bcrypt: 72). Enforced per call by
    # api/schemas._validate_password_bytes; raise it only together with a
    # hasher switch (scrypt/argon2), never on bcrypt.
    PASSWORD_MAX_BYTES: int = 72

    # Bootstrap admin (H5/H6/H28): credentials come from the environment, never
    # from source. When both username and password are set, the API provisions
    # this account at startup with a *bcrypt-hashed* password (the plaintext is
    # never accepted on the login path — login always goes through bcrypt).
    BOOTSTRAP_ADMIN_USERNAME: str = ""
    BOOTSTRAP_ADMIN_PASSWORD: str = ""
    BOOTSTRAP_ADMIN_EMAIL: str = ""

    # Reverse proxy (H12/H31): when the API sits behind a trusted proxy (nginx)
    # that sets ``X-Real-IP``, enable this so per-IP rate limiting buckets real
    # clients instead of collapsing everything into the proxy's address.
    TRUST_PROXY_HEADERS: bool = False

    # Export throttle (H18): minimum seconds between full exports per user.
    EXPORT_COOLDOWN_SECONDS: int = 300

    # M31: whole-stream export timeout in seconds (was the hardcoded
    # MAX_EXPORT_TIMEOUT magic constant). Guards against slow-client DoS;
    # a stream exceeding it is cut with a truncation sentinel row (M83).
    EXPORT_TIMEOUT_SECONDS: int = 300

    # Metrics endpoint (H19): when set, /metrics requires a matching
    # ``X-Metrics-Secret`` header. Empty keeps the legacy behaviour (network-
    # level gating only, e.g. the nginx allowlist).
    METRICS_SECRET: str = ""

    # SMTP (fail-soft alert email, see tasks/alerts.py). All empty by default so
    # email alerts are a no-op unless explicitly configured — never raises.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_USE_TLS: bool = True

    # Password reset (forgot-password flow). The expiry bounds how long a
    # one-time reset token stays valid; the link base is the frontend URL the
    # reset email points to (the backend has no HTML views of its own).
    PASSWORD_RESET_TOKEN_EXPIRY_MINUTES: int = 60
    PASSWORD_RESET_LINK_BASE: str = "http://localhost:5173/reset-password"

    # L9: fallback recipient for critical alert emails when no DB setting
    # exists (mirrors the other settings' config fallbacks in api/admin.py).
    ALERT_EMAIL: str = ""

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

    # L33: raw APP_ENV stashed by ``_capture_app_env`` below so field
    # validators can make production decisions without depending on field
    # declaration order — replaces the old hand-rolled ``.env`` parser.
    _app_env_raw: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _capture_app_env(cls, values):
        """Stash the raw APP_ENV before any field validation runs (L33).

        pydantic-settings has already merged ``os.environ`` and the ``.env``
        file into *values* by the time this runs, so no manual parsing is
        needed (the os.environ fallback only covers non-dict init paths).
        """
        app_env = None
        if isinstance(values, dict):
            app_env = values.get("APP_ENV") or values.get("app_env")
        if app_env is None:
            app_env = os.environ.get("APP_ENV")
        cls._app_env_raw = app_env
        return values

    @classmethod
    def _resolved_app_env(cls) -> str:
        """Effective APP_ENV for cross-field validation (defaults to development)."""
        return (cls._app_env_raw or "development").strip().lower()

    @field_validator("JWT_ALGORITHM")
    @classmethod
    def _validate_jwt_algorithm(cls, v: str) -> str:
        """Pin the JWT algorithm to HS256 (H4/M66).

        The decode side hardcodes ``["HS256"]`` regardless of this value; this
        validator exists so a misconfigured ``JWT_ALGORITHM=none`` (or any
        other algorithm) fails loudly at startup instead of silently changing
        the token contract.
        """
        if v != "HS256":
            raise ValueError(
                "JWT_ALGORITHM must be 'HS256' — the codebase pins both encode "
                f"and decode to HS256 (got {v!r})"
            )
        return v

    @field_validator("LOG_LEVEL")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}, got {v!r}")
        return upper

    @field_validator("MAX_CONTENT_LENGTH")
    @classmethod
    def _validate_max_content_length(cls, v: int) -> int:
        """Reject non-positive body caps (M5) — 0 would block every request."""
        if v <= 0:
            raise ValueError(f"MAX_CONTENT_LENGTH must be a positive byte count, got {v}")
        return v

    # M98: operational numerics must be positive — zero/negative values fail
    # silently downstream: dead-on-arrival JWT expiries (total auth outage),
    # ``Queue(maxsize<=0)`` becoming unbounded (no MQTT backpressure), or
    # exports truncating instantly.
    @field_validator(
        "JWT_ACCESS_TOKEN_EXPIRY_MINUTES",
        "JWT_REFRESH_TOKEN_EXPIRY_DAYS",
        "PASSWORD_RESET_TOKEN_EXPIRY_MINUTES",
        "EXPORT_TIMEOUT_SECONDS",
        "MQTT_QUEUE_MAX",
    )
    @classmethod
    def _validate_positive_operational_values(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be a positive value, got {v}")
        return v

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
        # M97: allow only schemes the installed sync driver can actually use —
        # postgresql+psycopg needs the uninstalled psycopg v3 and
        # postgresql+asyncpg breaks the sync engine with MissingGreenlet.
        if parsed.scheme not in {"postgresql", "postgres", "postgresql+psycopg2"}:
            raise ValueError(
                "DATABASE_URL must use scheme postgresql://, postgres:// or "
                f"postgresql+psycopg2://, got scheme {parsed.scheme!r} in {v!r}"
            )
        if not parsed.hostname:
            raise ValueError(f"DATABASE_URL must include a host, got {v!r}")
        # M73: ``postgresql://@localhost/db`` parses with an empty username —
        # reject it instead of letting the engine fail on first connect.
        if not parsed.username:
            raise ValueError(
                f"DATABASE_URL must include a username, got {v!r} "
                "(an empty user like postgresql://@host/db is rejected)"
            )
        # Password check is gated on APP_ENV, stashed by the mode="before"
        # validator so we don't depend on field ordering within the model (L33).
        app_env = cls._resolved_app_env()
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

        H1/H11: every non-wildcard entry must be a strict ``scheme://host[:port]``
        origin. This rejects ``null``, ``file://`` URLs, paths, and bare hosts —
        the cross-origin values browsers can actually send — instead of letting
        quart-cors reflect them verbatim. An empty allowlist in production is a
        hard error too (an empty list may degrade to a wildcard reflection in
        some quart-cors versions).
        """
        import re

        origins = [o.strip() for o in v.split(",") if o.strip()]
        app_env = cls._resolved_app_env()
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
        else:
            origin_re = re.compile(r"^https?://[A-Za-z0-9._-]+(:\d{1,5})?$")
            bad = [o for o in origins if not origin_re.fullmatch(o)]
            if bad:
                raise ValueError(
                    "CORS_ORIGINS entries must be origins of the form "
                    f"scheme://host[:port] — rejected: {bad!r} "
                    "(no 'null', file://, paths, or bare hostnames)"
                )
            if app_env == "production" and not origins:
                raise ValueError(
                    "CORS_ORIGINS is empty in production — refusing to start "
                    "with a possibly-wildcard CORS allowlist. Set explicit "
                    "origins (e.g. https://app.example.com)."
                )
        return v

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

        M67: the full length/entropy strength check is relaxed behind the
        explicit ``APP_ENV=test`` flag — test fixtures that rebuild Config
        after ``reset_config_cache()`` only pay for the cheap placeholder
        blocklist. ``test`` must be set deliberately (it is not a value any
        deployment uses), so a misspelled production APP_ENV still gets the
        full check.
        """
        check_strength = (self.APP_ENV or "").strip().lower() != "test"
        # Always reject known weak secrets, regardless of APP_ENV
        # This prevents silent security failures when APP_ENV is misspelled or unset
        bad = [
            name
            for name, value in (
                ("SECRET_KEY", self.SECRET_KEY),
                ("JWT_SECRET", self.JWT_SECRET),
            )
            if _is_weak_secret(value, check_strength=check_strength)
        ]
        # L66: a *set* bootstrap admin password gets the same gate — dev
        # placeholders are rejected in all environments and the full strength
        # check applies unless APP_ENV=test. Empty stays valid (the opt-out).
        if self.BOOTSTRAP_ADMIN_PASSWORD and _is_weak_secret(
            self.BOOTSTRAP_ADMIN_PASSWORD, check_strength=check_strength
        ):
            bad.append("BOOTSTRAP_ADMIN_PASSWORD")
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

        If TLS is enabled, certificate, key, AND CA bundle must be provided —
        the runtime ``tls_set()`` calls require all three (M68: the validator
        used to check only cert/key, so a missing ``MQTT_CA_CERTS`` passed
        startup and only failed later inside the client/publisher).
        """
        if self.MQTT_ENABLED and self.MQTT_USE_TLS and not (
            self.MQTT_TLS_CERT and self.MQTT_TLS_KEY and self.MQTT_CA_CERTS
        ):
            raise ValueError(
                "MQTT_TLS_CERT, MQTT_TLS_KEY and MQTT_CA_CERTS must all be set "
                "when MQTT_USE_TLS is True"
            )
        return self

    @model_validator(mode="after")
    def _validate_redis_url(self) -> "Config":
        """Validate Redis URL format (#43).

        Accepts ``redis://`` (plaintext), ``rediss://`` (TLS — L46, the old
        check forced plaintext Redis even for managed TLS brokers) and
        ``unix://`` (local socket) URLs.
        """
        if not self.REDIS_URL.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError(
                "REDIS_URL must be a redis://, rediss:// or unix:// URL"
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
    """Drop the cached Config so the next ``get_config()`` re-reads env/.env.

    M57: also drops the DB engines — they are built from ``DATABASE_URL``, so
    a reset that repoints the URL must not leave stale engines behind. The
    import is lazy to avoid a config ↔ models import cycle at startup.
    """
    global _config_cache
    _config_cache = None
    try:
        from models.base import reset_engines
    except ImportError:
        return
    reset_engines()
