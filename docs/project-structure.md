# Empyrean — Project Structure

This is the layout of the Empyrean backend repo. The tree below was verified against the actual repository contents, so it reflects the current state of the codebase rather than planned-but-unimplemented modules.

```
Empyrean-V2-Backend/
├── api/                    # Quart route handlers + JWT helpers + schemas (auth, profile)
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
├── fuzzy/                  # Tsukamoto fuzzy inference engine (empty — planned)
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
├── mqtt/                   # MQTT consumer & payload validation
├── scripts/                # Dev tools & utilities
│   ├── verify.py           # Full-stack verification (Python)
│   ├── check.bat           # Verify wrapper for cmd/PowerShell
│   ├── check_health.py     # Environment health check
│   ├── seed.py             # Dev seed script
│   └── db.sh               # Database helper (bash/Git Bash)
├── tasks/                  # Celery worker + beat task definitions (stubs)
│   ├── aggregation.py      # Aggregation & data-retention tasks (stub)
│   ├── alerts.py           # Alert threshold checks & broadcasts (stub)
│   └── forecast.py         # AQI forecasting (linear regression) (stub)
├── tests/                  # pytest suite
│   ├── __init__.py
│   ├── conftest.py         # Fixtures & test DB setup
│   └── test_smoke.py       # Smoke tests
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

- `api/` — Quart route handlers (auth, profile), JWT encode/decode + route-protection decorators, and Pydantic request/response schemas; `api/ws/` (WebSocket alert broadcasting) is currently empty — planned.
- `config/` — environment-based app configuration via pydantic-settings.
- `docs/` — project documentation (this file included).
- `fuzzy/` — Tsukamoto fuzzy inference engine (currently empty — planned).
- `migrations/` — Alembic environment (`env.py`) and versioned migration files.
- `models/` — SQLAlchemy ORM models (users, nodes, sensor readings, aggregates, alerts, settings) plus engine/session setup and shared helpers.
- `mqtt/` — MQTT consumer and payload validation.
- `scripts/` — dev tooling: full-stack verification, health check, seeding, and DB helpers.
- `tasks/` — Celery worker + beat task definitions (aggregation, alerts, forecasting) — currently stubs.
- `tests/` — pytest suite.
- `certs/` — MQTT TLS certificates (gitignored).

## Related Docs

- [architecture.md](architecture.md)
- [getting-started.md](getting-started.md)
- [README](../README.md)
