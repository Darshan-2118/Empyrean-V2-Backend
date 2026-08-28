# Empyrean — Configuration & Services

This document covers the environment variables the Empyrean backend reads at startup and the services that make up the single-system deployment. Configuration lives entirely in a `.env` file (see `.env.example`) — credentials and environment-specific values are never hardcoded in the codebase.

## Environment Variables

`.env.example` is the authoritative, fully-annotated list. Startup validation is strict: placeholder secrets (`change-me-*`, `dev-*`), short keys, and malformed URLs fail fast (see `config/__init__.py`).

| Variable | Example | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:root@localhost:5432/Empyrean` | PostgreSQL connection string (use `.env`, NOT tracked in git). Scheme must be `postgresql` / `postgres` / `postgresql+psycopg2` — async-driver schemes are rejected. |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `MQTT_ENABLED` | `false` | Master switch for MQTT ingestion — broker settings alone do not enable it |
| `MQTT_BROKER_HOST` | `localhost` | MQTT broker hostname |
| `MQTT_BROKER_PORT` | `1883` | MQTT broker port (non-TLS in dev) |
| `MQTT_USE_TLS` | `false` | Enable TLS for MQTT (requires certs) |
| `MQTT_TLS_CERT` | path to the client TLS cert | MQTT TLS client certificate |
| `MQTT_TLS_KEY` | path to the client TLS key | MQTT TLS client key |
| `MQTT_CA_CERTS` | path to the CA bundle | CA certificates for MQTT broker verification |
| `MQTT_CLIENT_ID` | _(empty)_ | Ingestion client id; empty derives `empyrean-backend-<hostname>`. Run only one client per host. |
| `MQTT_QUEUE_MAX` | `1000` | Max pending messages in the MQTT worker queue (backpressure bound; must be > 0) |
| `JWT_SECRET` | `<256-bit random>` | JWT signing secret (≥ 32 bytes, high entropy) |
| `JWT_ALGORITHM` | `HS256` | JWT algorithm — pinned to HS256, any other value rejected at startup |
| `JWT_ACCESS_TOKEN_EXPIRY_MINUTES` | `15` | Access-token lifetime (must be > 0) |
| `JWT_REFRESH_TOKEN_EXPIRY_DAYS` | `7` | Refresh-token lifetime (must be > 0) |
| `PASSWORD_MAX_BYTES` | `72` | Max accepted password size in UTF-8 bytes (bcrypt limit) |
| `BOOTSTRAP_ADMIN_USERNAME` | _(empty)_ | Optional admin auto-provisioned at startup/seed when set together with the password |
| `BOOTSTRAP_ADMIN_PASSWORD` | _(empty)_ | Bootstrap admin password — must pass the strength gate (≥ 8 chars, mixed case, digit, symbol) |
| `BOOTSTRAP_ADMIN_EMAIL` | _(empty)_ | Optional email for the bootstrap admin |
| `TRUST_PROXY_HEADERS` | `false` | Trust `X-Real-IP` from a reverse proxy — enable ONLY behind a trusted proxy (see `deploy/nginx.conf`) |
| `EXPORT_COOLDOWN_SECONDS` | `300` | Minimum seconds between exports per user |
| `EXPORT_TIMEOUT_SECONDS` | `300` | Whole-stream export timeout in seconds (must be > 0) |
| `METRICS_SECRET` | _(empty)_ | When set, `/metrics` requires a matching `X-Metrics-Secret` header |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_USE_TLS` | _(empty)_ / `587` / … | Fail-soft alert email; all empty by default so email alerts are a no-op unless configured |
| `ALERT_EMAIL` | _(empty)_ | Fallback recipient for critical alert emails when no DB setting exists |
| `AQI_WARNING_THRESHOLD` | `100` | AQI value that triggers a warning alert |
| `AQI_CRITICAL_THRESHOLD` | `150` | AQI value that triggers a critical alert |
| `DATA_RETENTION_DAYS` | `365` | Retention window (days) for the data-retention cleanup task |
| `TASK_SOFT_TIME_LIMIT` | `300` | Celery soft task timeout in seconds (must stay below the hard limit) |
| `TASK_HARD_TIME_LIMIT` | `600` | Celery hard task timeout in seconds |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Comma-separated allowed frontend origins |
| `APP_ENV` | `development` | Environment: `development`, `production`, or `test` |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG/INFO/WARNING/ERROR/CRITICAL) |
| `SECRET_KEY` | `<256-bit random>` | Application secret key |
| `MAX_CONTENT_LENGTH` | `65536` | HTTP request body cap in bytes; larger bodies get `413` |
| `OTLP_ENDPOINT` | _(unset)_ | Optional OTLP gRPC endpoint (e.g. `http://localhost:4317`). When unset, OpenTelemetry span export is disabled and no per-request span JSON is written to stdout. |

## Services (single-system deployment)

All services run as local processes on one host — no containers. Use a process manager (`systemd`, `supervisord`, or `pm2`) in production to keep them running and restart on failure.

| Service | Run as | Port | Notes |
|---|---|---|---|
| `quart-api` | `hypercorn "app:create_app()"` | 8000 | Quart async API server |
| `celery-worker` | `celery -A celery_app.celery_app worker` | — | Fuzzy inference + ML tasks |
| `celery-beat` | `celery -A celery_app.celery_app beat` | — | Scheduled aggregation + alert checks |
| `mosquitto` | native install / systemd service | 8883 (TLS) / 1883 (dev) | MQTT broker (TLS in prod, plaintext in dev) |
| `timescaledb` | native PostgreSQL + TimescaleDB extension | 5432 | Primary database |
| `redis` | native install / systemd service | 6379 | Cache + Celery broker |

## Related Docs

- [getting-started.md](getting-started.md)
- [security.md](security.md)
- [README](../README.md)
