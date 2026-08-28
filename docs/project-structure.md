# Empyrean — Project Structure

This is the layout of the Empyrean backend repo. The tree below was verified against the actual repository contents, so it reflects the current state of the codebase rather than planned-but-unimplemented modules.

```
Empyrean-V2-Backend/
├── api/                    # Quart route handlers + JWT helpers + schemas + Redis cache/rate-limit + middleware
│   ├── auth.py             # Register/login/refresh/logout
│   ├── jwt.py              # JWT encode/decode + @jwt_required / @admin_required
│   ├── profile.py          # Profile CRUD + change password
│   ├── cache.py            # Redis read-through cache helpers (readings/forecast)
│   ├── rate_limit.py       # @rate_limit Redis fixed-window decorator
│   ├── validation.py       # @validate_body Pydantic request-body middleware (Phase 12)
│   ├── request_log.py      # App-wide request logging: method/path/status/duration (Phase 12)
│   ├── readings.py         # GET /readings/latest + /readings/history
│   ├── forecast.py         # GET /forecast (60-min AQI prediction)
│   ├── nodes.py            # GET/POST /nodes + PATCH /nodes/:node_id
│   ├── alerts.py           # GET /alerts + PATCH /alerts/:alert_id/acknowledge
│   ├── admin.py            # Admin: /admin/health + /admin/settings (GET/PATCH), settings registry
│   ├── export.py           # GET /export — streaming CSV download of raw readings (Phase 11)
│   ├── metrics.py          # Prometheus metrics: /metrics endpoint + counters/histograms (Phase 14)
│   ├── _time.py            # Shared ISO-8601 query-param parser (parse_iso_datetime)
│   ├── schemas.py          # Pydantic request/response DTOs
│   └── ws/                 # WebSocket alert broadcasting (manager + routes)
│       ├── manager.py      # Thread-safe connection manager — broadcast from MQTT thread
│       └── routes.py       # /ws/alerts endpoint — JWT auth before accept
├── config/
│   └── __init__.py         # App configuration (pydantic-settings, Dev/Prod)
├── deploy/                 # Production deployment artifacts (Phase 14)
│   ├── quart-api.service   # systemd unit — hypercorn app:create_app() on 127.0.0.1:8000
│   ├── celery-worker.service  # systemd unit — celery_app.celery_app worker
│   ├── celery-beat.service    # systemd unit — celery_app.celery_app beat
│   ├── nginx.conf          # Nginx TLS termination 443→8000, /ws upgrade, /metrics restricted to 127.0.0.1
│   ├── logrotate           # logrotate config for /var/log/nginx/ + app journal
│   ├── deploy.sh           # Idempotent rsync + systemctl + alembic upgrade
│   └── .env.production.example  # Template with SECRET_KEY, JWT_SECRET, DATABASE_URL, REDIS_URL, MQTT_BROKER_HOST
├── docs/                   # Project documentation
│   ├── api.md
│   ├── architecture.md
│   ├── bugs.md             # Audit findings ledger (all closed)
│   ├── configuration.md
│   ├── database.md
│   ├── database-setup.md   # PostgreSQL & TimescaleDB setup guide
│   ├── FIXED_BUGS.md       # Fix log for every closed audit finding
│   ├── frontend-data.md
│   ├── fuzzy-engine.md
│   ├── getting-started.md  # Local setup + production deployment
│   ├── overview.md         # Comprehensive system overview & purpose
│   ├── project-structure.md
│   ├── security.md
│   └── testing.md          # Manual testing & hardware simulation guide
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
│   ├── client.py           # paho MQTT client — reads readings/status, dispatches to Celery, bridges air/alerts to WS
│   ├── validator.py        # Pydantic payload validation (ReadingPayload / StatusPayload)
│   ├── config.py           # publish_config — push device config over MQTT
│   └── publisher.py        # fire-and-forget paho publisher — Celery worker publishes air/alerts broadcasts
├── scripts/                # Dev tools & utilities
│   ├── start.bat           # Auto-starts Redis & launches WSL Instance/Worker/Beat/Server in Windows Terminal tabs
│   ├── stop.bat            # Stops all dev services & shuts down Redis
│   ├── generate_secrets.py # Cryptographic secret generator for .env (SECRET_KEY, JWT_SECRET)
│   ├── verify.py           # Full-stack verification (Python)
│   ├── check.bat           # Verify wrapper for cmd/PowerShell
│   ├── check_health.py     # Environment health check
│   ├── seed.py             # Dev seed script
│   ├── create_admin.py     # Interactive admin account creation (no hardcoded creds)
│   ├── db.sh               # Database helper (bash/Git Bash)
│   └── bench.py            # Performance load generator (Phase 14)
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
│   ├── test_fuzzy.py       # Fuzzy engine unit tests (pure, no DB/Redis)
│   ├── test_admin.py       # Phase 10 — settings registry, schema, fail-soft health, retention wiring
│   ├── test_metrics.py     # Prometheus /metrics endpoint + counter/histogram labels (Phase 14)
│   ├── test_bench.py       # bench.py smoke — runs against /health (Phase 14)
│   └── test_deployment.py  # deploy/ artifact gate (Phase 14)
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

- `api/` — Quart route handlers (auth, profile, readings, forecast, nodes, alerts, admin, export), JWT encode/decode + route-protection decorators, Redis read-through cache helpers (`cache.py`) and rate-limit decorator (`rate_limit.py`), the shared ISO-8601 query-param parser (`_time.py`), and Pydantic request/response schemas; `api/ws/` holds the WebSocket alert broadcasting layer (thread-safe connection manager + JWT-authenticated `/ws/alerts` endpoint). `admin.py` is the Phase 10 admin tier: fail-soft system health plus the `system_settings` registry (GET/PATCH, admin-only). `export.py` is the Phase 11 streaming CSV download of raw readings (`/export`). Phase 12 adds the request-body validation middleware (`validation.py`, the `@validate_body` decorator applied to every JSON-body endpoint) and the app-wide request-logging hooks (`request_log.py`, one `empyrean.request` line per HTTP request).
- `config/` — environment-based app configuration via pydantic-settings.
- `docs/` — project documentation (this file included).
- `fuzzy/` — Tsukamoto fuzzy inference engine: membership functions (`membership.py`), the 27-rule base + consequent ramps (`rules.py`), and defuzzification + `infer()`/`fuzzy_score()` entrypoints (`tsukamoto.py`).
- `migrations/` — Alembic environment (`env.py`) and versioned migration files.
- `models/` — SQLAlchemy ORM models (users, nodes, sensor readings, aggregates, alerts, settings) plus engine/session setup and shared helpers.
- `mqtt/` — MQTT ingestion: paho client (`client.py`) subscribing to `air/node/+/reading` and `air/node/+/status`, dispatching validated readings to the Celery `process_reading` task and updating `Node.last_seen` on heartbeats, and bridging `air/alerts` broadcasts to WebSocket clients; Pydantic payload validation (`validator.py`); device config publisher (`config.py`); fire-and-forget `air/alerts` publisher (`publisher.py`) used by the Celery alert task.
- `scripts/` — dev tooling: full-stack verification, health check, seeding, DB helpers, and a performance load generator (`bench.py`).
- `tasks/` — Celery worker + beat task definitions: per-reading enrichment (`process_reading.py`, `aqi.py`), hourly aggregation + data retention (`aggregation.py`), alert threshold checks (`alerts.py`), and linear-regression AQI forecasting (`forecast.py`).
- `tests/` — pytest suite.
- `certs/` — MQTT TLS certificates (gitignored).

## Related Docs

- [architecture.md](architecture.md)
- [getting-started.md](getting-started.md)
- [README](../README.md)
