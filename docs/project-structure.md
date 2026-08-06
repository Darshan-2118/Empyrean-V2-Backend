# Empyrean — Project Structure

This is the layout of the Empyrean backend repo. The tree below was verified against the actual repository contents, so it reflects the current state of the codebase rather than planned-but-unimplemented modules.

```
Empyrean-V2-Backend/
├── api/                    # Quart route handlers + JWT helpers + schemas + Redis cache/rate-limit
│   ├── auth.py             # Register/login/refresh/logout
│   ├── jwt.py              # JWT encode/decode + @jwt_required / @admin_required
│   ├── profile.py          # Profile CRUD + change password
│   ├── cache.py            # Redis read-through cache helpers (readings/forecast)
│   ├── rate_limit.py       # @rate_limit Redis fixed-window decorator
│   ├── readings.py         # GET /readings/latest + /readings/history
│   ├── forecast.py         # GET /forecast (60-min AQI prediction)
│   ├── schemas.py          # Pydantic request/response DTOs
│   └── ws/                 # WebSocket alert broadcasting (empty — planned)
├── config/
│   └── __init__.py         # App configuration (pydantic-settings, Dev/Prod)
├── docs/                   # Project documentation
│   ├── TODO.md
│   ├── api.md
│   ├── architecture.md
│   ├── configuration.md
│   ├── database.md
│   ├── frontend-integration.md
│   ├── fuzzy-engine.md
│   ├── getting-started.md
│   ├── mqtt.md
│   ├── project-structure.md
│   ├── schema-plan.md      # Database schema blueprint
│   └── security.md
├── fuzzy/                  # Tsukamoto fuzzy inference engine
│   ├── __init__.py         # Public API re-exports (fuzzy_score, infer)
│   ├── membership.py       # Membership functions (triangular/trapezoidal)
│   ├── rules.py            # 27-rule base + monotonic consequent ramps
│   └── tsukamoto.py        # Defuzzification (weighted average) + infer()/fuzzy_score()
├── migrations/
│   ├── env.py              # Alembic environment (wired to models)
│   ├── script.py.mako      # Migration template
│   └── versions/           # Migration files
├── models/
│   ├── __init__.py         # Re-exports all models + base utilities
│   ├── base.py             # SQLAlchemy engine, session factories, retry logic
│   ├── helpers.py          # Shared utilities (password hashing)
│   ├── user.py             # User model
│   ├── refresh_token.py    # RefreshToken model
│   ├── node.py             # Node model (ESP32 sensor devices)
│   ├── reading.py          # SensorReading model (TimescaleDB hypertable)
│   ├── aggregate.py        # HourlyAgg model
│   ├── alert.py            # Alert model
│   └── setting.py          # SystemSetting model
├── mqtt/                   # MQTT ingestion consumer & payload validation
│   ├── client.py           # paho MQTT client — subscribes to readings/status, dispatches to Celery
│   ├── validator.py        # Pydantic payload validation (ReadingPayload / StatusPayload)
│   └── config.py           # publish_config — push device config over MQTT
├── scripts/                # Dev tools & utilities
│   ├── verify.py           # Full-stack verification (Python)
│   ├── check.bat           # Verify wrapper for cmd/PowerShell
│   ├── check_health.py     # Environment health check
│   ├── seed.py             # Dev seed script
│   ├── smoke_phases.py     # TEMPORARY phase 1–7 health/working smoke (replaced in Phase 13)
│   └── db.sh               # Database helper (bash/Git Bash)
├── tasks/                  # Celery worker + beat task definitions
│   ├── aqi.py              # EPA AQI computation from PM2.5/PM10 (pure math)
│   ├── process_reading.py  # Per-reading enrichment: fuzzy → AQI → anomaly → persist → cache
│   ├── aggregation.py      # Hourly aggregation + data-retention cleanup
│   ├── alerts.py           # AQI threshold checks & alert creation
│   └── forecast.py         # AQI forecasting (linear regression) + generate_forecast helper
├── tests/                  # pytest suite
│   ├── __init__.py
│   ├── conftest.py         # Fixtures & test DB setup
│   ├── test_smoke.py       # Smoke tests
│   └── test_fuzzy.py       # Fuzzy engine unit tests (pure, no DB/Redis)
├── certs/                  # MQTT TLS certificates (gitignored)
├── app.py                  # Quart application factory
├── celery_app.py           # Celery application instance
├── requirements.txt
├── alembic.ini             # Alembic configuration
├── .gitignore
├── .env                    # Local credentials (gitignored)
└── .env.example            # Template env vars (safe to commit)
```

Key top-level directories:

- `api/` — Quart route handlers (auth, profile, readings, forecast), JWT encode/decode + route-protection decorators, Redis read-through cache helpers (`cache.py`) and rate-limit decorator (`rate_limit.py`), and Pydantic request/response schemas; `api/ws/` (WebSocket alert broadcasting) is currently empty — planned.
- `config/` — environment-based app configuration via pydantic-settings.
- `docs/` — project documentation (this file included).
- `fuzzy/` — Tsukamoto fuzzy inference engine: membership functions (`membership.py`), the 27-rule base + consequent ramps (`rules.py`), and defuzzification + `infer()`/`fuzzy_score()` entrypoints (`tsukamoto.py`).
- `migrations/` — Alembic environment (`env.py`) and versioned migration files.
- `models/` — SQLAlchemy ORM models (users, nodes, sensor readings, aggregates, alerts, settings) plus engine/session setup and shared helpers.
- `mqtt/` — MQTT ingestion: paho client (`client.py`) subscribing to `air/node/+/reading` and `air/node/+/status`, dispatching validated readings to the Celery `process_reading` task and updating `Node.last_seen` on heartbeats; Pydantic payload validation (`validator.py`); device config publisher (`config.py`).
- `scripts/` — dev tooling: full-stack verification, health check, seeding, DB helpers, and a temporary phase-1–7 smoke (`smoke_phases.py`).
- `tasks/` — Celery worker + beat task definitions: per-reading enrichment (`process_reading.py`, `aqi.py`), hourly aggregation + data retention (`aggregation.py`), alert threshold checks (`alerts.py`), and linear-regression AQI forecasting (`forecast.py`).
- `tests/` — pytest suite.
- `certs/` — MQTT TLS certificates (gitignored).

## Related Docs

- [architecture.md](architecture.md)
- [getting-started.md](getting-started.md)
- [README](../README.md)
