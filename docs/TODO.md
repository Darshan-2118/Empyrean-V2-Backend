# Empyrean V2 Backend — Project TODO

## Overview
Complete implementation checklist for the Empyrean IoT Air Quality Mapping System backend. Each phase builds on the previous one.

> **Status 2026-08-11:** Phases 1–11 are complete and the phase-regression backlog in `docs/known-issues.md` is **fully resolved** (68 `FIXED`, 1 `WONTDO`/accepted; 0 open) — verified by a full `pytest -q` gate (**210 passed** on 2026-08-07, with the Phase 11 suites `tests/test_export.py` and `test_phase_1_to_11_export` added on top). Phases 12–14 below remain.
>
> **Health smoke (temporary):** `scripts/smoke_phases.py` runs a lightweight phase 1–11 health/working check (imports, app factory, routes, JWT, MQTT validation+dispatch with a stubbed broker, fuzzy inference, Celery task registration, Nodes API routes + registry/schemas, Alerts/WS + Admin routes, settings registry + schema, Export CSV generator + shared ISO parser). It is a stopgap and will be replaced by the full Smoke/verification script in Phase 13. The cumulative behavioral harness is `tests/test_phase_coverage.py` (12 tests, phases 1–11) plus the focused suites `tests/test_export.py` (Phase 11), `tests/test_admin.py` (Phase 10) and `tests/test_alerts.py` (Phase 9).

---

## Phase 1: Project Scaffolding & Core Infrastructure

- [x] **Initialize project structure** — create all directories: `api/`, `fuzzy/`, `tasks/`, `models/`, `mqtt/`, `api/ws/`, `migrations/`, `tests/`, `config/`
- [x] **Create `.gitignore`** — Python, venv, `.env`, `__pycache__`, `.DS_Store`, etc.
- [x] **Create `requirements.txt`** — list all dependencies (Quart, Quart-CORS, SQLAlchemy, asyncpg, psycopg2, alembic, celery[redis], redis, paho-mqtt, PyJWT, bcrypt, scikit-learn, pandas, pydantic, pytest, etc.)
- [x] **Create `.env.example`** — template for all environment variables (DATABASE_URL, REDIS_URL, MQTT_BROKER_HOST, JWT_SECRET, etc.)
- [x] **Create `app.py`** — Quart application factory, register blueprints, CORS config, error handlers
- [x] **Create `config/__init__.py`** — load env vars via pydantic-settings, app configuration classes (Dev/Prod)
- [x] **Create `celery_app.py`** — Celery application instance with Redis broker config
- [x] **Set up Alembic** — `alembic.ini` + `migrations/` directory for DB schema versioning
- [x] **Set up logging** — structured logging config for all services
- [x] ~~**Set up CI/CD** — GitHub Actions workflow (test on push, deploy on tag)~~ _(removed — not needed for this project)_

---

## Phase 2: Database Configuration, Models & Migrations

- [x] **DB connection & session setup** — `models/base.py` — configure SQLAlchemy engine, session factory, connection pooling (asyncpg for async, psycopg2 for sync/Celery)
- [x] **Create SQLAlchemy base** — `models/__init__.py` with declarative base and metadata
- [x] **User model** — `models/user.py` (id, username, email, password_hash, role, notification_prefs, is_active, last_login_at, created_at, updated_at)
- [x] **Refresh Token model** — `models/refresh_token.py` (id, user_id FK, token_hash, expires_at, created_at, revoked)
- [x] **Nodes model** — `models/node.py` (node_id PK, name, location_name, lat, lon, firmware_version, reading_interval, is_active, registered_at, last_seen)
- [x] **Sensor Readings model** — `models/reading.py` (time, node_id FK, temperature, humidity, pressure, voc_ohm, mq135_ppm, pm1, pm25, pm10, battery_v, fuzzy_score, aqi, aqi_category, is_anomaly) — regular table (TimescaleDB hypertable later)
- [x] **Hourly Aggregate model** — `models/aggregate.py` (bucket, node_id, avg_temperature, avg_humidity, avg_pm25, avg_pm10, max_aqi, min_aqi, avg_aqi, anomaly_count, reading_count) — regular table (continuous aggregate later)
- [x] **Alerts model** — `models/alert.py` (alert_id PK, node_id FK, parameter, value, threshold, severity, message, triggered_at, acknowledged_at, acknowledged_by FK)
- [x] **System Settings model** — `models/setting.py` (key PK, value, description, updated_at, updated_by FK)
- [x] **Create Alembic migrations** — initial schema with all 7 tables, indexes on refresh_tokens and sensor_readings
- [x] **TimescaleDB hypertable migration** — converts `sensor_readings` to a hypertable (`b2bab23ab3c0`)
- [x] **Seed script** — `scripts/seed.py` for dev/test data (admin user, default settings, sample nodes)
- [x] **Health check script** — `scripts/check_health.py` validates entire stack
- [x] **Code review** — fixed asyncio deprecation, CWD path bugs, model-migration drift

---

## Phase 3: Authentication & User Management

### Auth Endpoints
- [x] **POST `/api/v1/auth/register`** — user registration (auto-login — returns JWT tokens immediately)
- [x] **POST `/api/v1/auth/login`** — authenticate, return JWT access + refresh tokens
- [x] **POST `/api/v1/auth/refresh`** — exchange refresh token for new access token (token rotation — revokes old)
- [x] **POST `/api/v1/auth/logout`** — invalidate refresh token (returns 204, no info leakage)

### JWT Middleware
- [x] **JWT encoding/decoding utility** — HS256 token creation, validation, expiry checks (`api/jwt.py`)
- [x] **Auth decorator** — `@jwt_required` decorator for protected routes
- [x] **Admin-only decorator** — `@admin_required` for admin endpoints
- [x] **Refresh token rotation** — secure refresh token handling (revoke old → issue new)

### Profile Endpoints
- [x] **GET `/api/v1/profile`** — get own profile
- [x] **PATCH `/api/v1/profile`** — update username/email/notification prefs
- [x] **POST `/api/v1/profile/change-password`** — change password (bcrypt verify + rehash)
- [x] **DELETE `/api/v1/profile`** — delete own account (set is_active=false, revoke all tokens)

---

## Phase 4: MQTT Ingestion Layer

- [x] **MQTT client module** — `mqtt/client.py` — MQTT client connecting to Mosquitto broker over TLS
- [x] **Payload validation** — `mqtt/validator.py` — Pydantic/JSON schema validation for incoming sensor readings
- [x] **Topic handler** — subscribe to `air/node/{id}/reading`, `air/node/{id}/status`
- [x] **Reading ingestion flow** — receive MQTT message → validate → dispatch to Celery worker for processing
- [x] **Status heartbeat handler** — update `last_seen` in nodes table on status messages
- [x] **MQTT config publisher** — `mqtt/config.py` — publish config changes to `air/node/{id}/config`
- [x] **TLS certificate handling** — client certificate auth for device connections
- [x] **QoS management** — at-least-once delivery with retry logic

---

## Phase 5: Sensor Readings API

- [x] **GET `/api/v1/readings/latest`** — latest reading per node (Redis-cached, TTL 60s)
- [x] **GET `/api/v1/readings/history`** — time-bucketed historical readings (`from`, `to`, `node_id`, `bucket` params)
- [x] **Redis caching layer** — `api/cache.py` — read-through cache pattern for readings
- [x] **DTO schemas** — Pydantic models for request validation and response serialization
- [x] **Rate limiting middleware** — Redis-based, 200 req/min per IP, `X-RateLimit-*` headers

---

## Phase 6: Tsukamoto Fuzzy Inference Engine

- [x] **Membership functions** — `fuzzy/membership.py` — triangular/trapezoidal MFs for Temperature, Humidity, PM2.5
  - Temperature: Low (peak 20, support [0–30]), Medium (25–35–45), High (ramp 40→50)
  - Humidity: Dry (shoulder [0–25]), Humid (40–60–80), Wet (ramp 60→80)
  - PM2.5: Low (shoulder [0–25]), Medium (40–60–80), High (ramp 70→90)
- [x] **Fuzzy rules engine** — `fuzzy/rules.py` — full 27-rule rule base evaluation
- [x] **Defuzzification** — `fuzzy/tsukamoto.py` — weighted-average defuzzification: `AQI_crisp = Σ(αᵢ × zᵢ) / Σ(αᵢ)`
- [x] **Fuzzy inference pipeline** — compose membership → rules → defuzzification into single call
- [x] **Unit tests** — `tests/test_fuzzy.py` — edge cases, boundary conditions, known output validation

---

## Phase 7: Celery Tasks (Async Processing)

### Fuzzy Inference & Enrichment
- [x] **Process reading task** — consume raw sensor reading, run fuzzy inference, compute AQI, detect anomalies
- [x] **AQI computation** — EPA AQI from PM2.5/PM10 (standard breakpoints)
- [x] **Anomaly detection** — Z-score based anomaly flagging

### Scheduled Tasks (Celery Beat)
- [x] **Hourly aggregation** — compute `hourly_agg` via UPSERT of the last complete hour (regular table for now)
- [x] **Alert threshold check** — every 60s, check AQI against thresholds, create alert records on breach
- [x] **Forecast model retraining** — hourly retrain of linear regression model
- [x] **Data retention cleanup** — enforce 1-year retention policy

### Forecast
- [x] **Forecast generation task** — linear regression for next-60-minute AQI prediction
- [x] **GET `/api/v1/forecast`** — API endpoint for forecast data (Redis-cached, TTL 1h)

---

## Phase 8: Nodes API

- [x] **GET `/api/v1/nodes`** — all registered nodes with metadata (Redis-cached, TTL 300s)
- [x] **PATCH `/api/v1/nodes/:node_id`** — update node config (Admin only; pushes to device via MQTT)
- [x] **Node registration endpoint** — POST `/api/v1/nodes` — register a new sensor node

---

## Phase 9: Alerts & WebSocket

### Alerts API
- [x] **GET `/api/v1/alerts`** — list unacknowledged alerts (with `limit`, `offset`, `severity` filters)
- [x] **PATCH `/api/v1/alerts/:alert_id/acknowledge`** — mark alert as acknowledged
- [x] **Alert creation logic** — triggered by Celery Beat when AQI exceeds thresholds

### WebSocket
- [x] **WebSocket connection manager** — `api/ws/manager.py` — track connected clients, handle lifecycle
- [x] **Alert broadcasting** — bridge MQTT `air/alerts` topic to WebSocket clients
- [x] **WebSocket auth** — authenticate WebSocket connections via JWT

---

## Phase 10: Admin Endpoints

- [x] **GET `/api/v1/admin/health`** — system health check (MQTT, TimescaleDB, Redis, Celery worker/beat, DB & Redis size)
- [x] **GET `/api/v1/admin/settings`** — view system settings (Admin only)
- [x] **PATCH `/api/v1/admin/settings`** — update AQI thresholds, data retention, alert config
- [x] **Admin middleware** — role-based access control for admin routes

---

## Phase 11: Export & Utilities

- [x] **GET `/api/v1/export`** — CSV download of raw readings for a date range (streaming, span-capped at 365 days, any authenticated user)
- [x] **CSV generator** — streaming CSV response for large exports (~64 KB chunks from a server-side cursor)
- [x] **Health check endpoint** — already satisfied by the liveness `/health` (app.py) and `/api/v1/admin/health` (api/admin.py, Phase 10); no new work was needed

---

## Phase 12: Error Handling & Middleware

- [x] **RFC 7807 Problem JSON** — standardized error responses (`application/problem+json`)
- [x] **Global error handlers** — 400, 401, 403, 404, 422, 429, 500 error handlers
- [ ] **Request validation middleware** — Pydantic schema validation on all inputs (partial)
- [x] **CORS configuration** — allow frontend origins, credentials support
- [ ] **Request logging middleware** — log method, path, status, duration

---

## Phase 13: Testing

- [ ] **Test configuration** — `tests/conftest.py` with fixtures, test DB, test Redis
- [ ] **Auth tests** — registration, login, token refresh, profile CRUD, edge cases
- [ ] **Readings API tests** — latest, history, caching behavior, invalid params
- [ ] **Nodes API tests** — list nodes, update node, admin auth checks
- [ ] **Alerts API tests** — list, acknowledge, filter, pagination
- [ ] **Forecast API tests** — forecast retrieval, caching
- [ ] **Admin tests** — health check, settings CRUD, authorization
- [ ] **Export tests** — CSV generation, date filtering
- [ ] **Fuzzy engine tests** — unit tests for membership functions, rules, defuzzification
- [ ] **MQTT tests** — payload validation, malformed messages
- [ ] **Celery task tests** — process reading, aggregation, alert checking
- [ ] **Integration tests** — end-to-end flows (MQTT → Celery → DB → API)

---

## Phase 14: Deployment & Production Readiness

- [ ] **systemd unit files** — `deploy/quart-api.service`, `deploy/celery-worker.service`, `deploy/celery-beat.service`
- [ ] **Nginx/Caddy reverse proxy config** — TLS termination for HTTPS
- [ ] **Log rotation config** — logrotate rules for all services
- [ ] **Production env checklist** — secure secret generation, DB setup, MQTT TLS certs
- [ ] **Deployment script** — `deploy/deploy.sh` — rsync + systemctl restart
- [ ] **Monitoring setup** — health check endpoint, basic metrics
- [ ] **Performance testing** — verify targets: <2s e2e latency, <200ms API p95, ≥50 nodes, ≥100 RPS

---

## Milestone Summary

| Milestone | Description | Depends On |
|-----------|-------------|------------|
| **M1: Foundation** | Project scaffolding, config, DB config, models, migrations ✅ | — |
| **M2: Auth** | Registration, login, JWT, profile management ✅ | M1 |
| **M3: Ingestion** | MQTT consumer, payload validation, reading intake ✅ | M1 |
| **M4: Core Processing** | Fuzzy inference engine, Celery tasks, AQI computation ✅ | M2, M3 |
| **M5: API Layer** | Readings + forecast + nodes + alerts + export live ✅ | M2, M4 |
| **M6: Real-time** | WebSocket alert broadcasting ✅ | M5 |
| **M7: Admin & Ops** | Admin endpoints, health monitoring ✅ | M5 |
| **M8: Hardening** | Tests, error handling, production deployment | M1–M7 |
| **M8.1 (2026-08-06)** | Known-issues backlog resolved (C/H/M/L/N tiers) ✅ · `pytest -q` = 155 passed · temp phase smoke `scripts/smoke_phases.py` · Phase 13 exhaustive testing deferred | M1–M7 |

---

*Generated from architecture spec in README.md*
