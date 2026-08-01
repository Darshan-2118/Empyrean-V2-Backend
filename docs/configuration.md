# Empyrean — Configuration & Services

This document covers the environment variables the Empyrean backend reads at startup and the services that make up the single-system deployment. Configuration lives entirely in a `.env` file (see `.env.example`) — credentials and environment-specific values are never hardcoded in the codebase.

## Environment Variables

| Variable | Example | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:root@localhost:5432/Empyrean` | PostgreSQL connection string (use `.env`, NOT tracked in git) |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `MQTT_BROKER_HOST` | `localhost` | MQTT broker hostname |
| `MQTT_BROKER_PORT` | `1883` | MQTT broker port (non-TLS in dev) |
| `MQTT_USE_TLS` | `false` | Enable TLS for MQTT (requires certs) |
| `JWT_SECRET` | `<256-bit random>` | JWT signing secret |
| `JWT_ALGORITHM` | `HS256` | JWT algorithm |
| `AQI_WARNING_THRESHOLD` | `100` | AQI value that triggers a warning alert |
| `AQI_CRITICAL_THRESHOLD` | `150` | AQI value that triggers a critical alert |
| `MQTT_TLS_CERT` | path to the client TLS cert | MQTT TLS client certificate |
| `MQTT_TLS_KEY` | path to the client TLS key | MQTT TLS client key |
| `MQTT_CA_CERTS` | path to the CA bundle | CA certificates for MQTT broker verification |
| `DATA_RETENTION_DAYS` | `365` | Retention window (days) for the data-retention cleanup task |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Comma-separated allowed frontend origins |
| `APP_ENV` | `development` | Environment: `development` or `production` |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG/INFO/WARNING/ERROR/CRITICAL) |
| `SECRET_KEY` | `<256-bit random>` | Application secret key |

## Services (single-system deployment)

All services run as local processes on one host — no containers. Use a process manager (`systemd`, `supervisord`, or `pm2`) in production to keep them running and restart on failure.

| Service | Run as | Port | Notes |
|---|---|---|---|
| `quart-api` | `hypercorn app:app` (or `quart run`) | 8000 | Quart async API server |
| `celery-worker` | `celery -A celery_app worker` | — | Fuzzy inference + ML tasks |
| `celery-beat` | `celery -A celery_app beat` | — | Scheduled aggregation + alert checks |
| `mosquitto` | native install / systemd service | 8883 (TLS) / 1883 (dev) | MQTT broker (TLS in prod, plaintext in dev) |
| `timescaledb` | native PostgreSQL + TimescaleDB extension | 5432 | Primary database |
| `redis` | native install / systemd service | 6379 | Cache + Celery broker |

## Related Docs

- [getting-started.md](getting-started.md)
- [security.md](security.md)
- [README](../README.md)
