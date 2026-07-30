# Empyrean V2 Backend — Project TODO

## Overview
Complete implementation checklist for the Empyrean IoT Air Quality Mapping System backend. Each phase builds on the previous one.

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
- [x] **Hourly Aggregate model** — `models/aggregate.py` (bucket, node_id, avg_temperature, avg_humidity, avg_pm25, avg_pm10, max_aqi, min_aqi, avg_aqi, anomaly_count, reading_count) — regular materialized view (continuous aggregate later)
- [x] **Alerts model** — `models/alert.py` (alert_id PK, node_id FK, parameter, value, threshold, severity, message, triggered_at, acknowledged_at, acknowledged_by FK)
- [x] **System Settings model** — `models/setting.py` (key PK, value, description, updated_at, updated_by FK)
- [x] **Create Alembic migrations** — initial schema with all 7 tables, indexes on refresh_tokens and sensor_readings
- [x] **Seed script** — `scripts/seed.py` for dev/test data (admin user, default settings, sample nodes)
- [x] **Health check script** — `scripts/check_health.py` validates entire stack
- [x] **Code review** — fixed asyncio deprecation, CWD path bugs, model-migration drift

---

## Phase 3: Authentication & User Management

### Auth Endpoints
- [ ] **POST `/api/v1/auth/register`** — user registration (username, email, password, optional profile fields)
- [ ] **POST `/api/v1/auth/login`** — authenticate, return JWT access + refresh tokens
- [ ] **POST `/api/v1/auth/refresh`** — exchange refresh token for new access token
- [ ] **POST `/api/v1/auth/logout`** — invalidate refresh token

### JWT Middleware
- [ ] **JWT encoding/decoding utility** — RS256 signing, token validation, expiry checks
- [ ] **Auth decorator** — `@jwt_required` decorator for protected routes
- [ ] **Admin-only decorator** — `@admin_required` for admin endpoints
- [ ] **Refresh token rotation** — secure refresh token handling

### Profile Endpoints
- [ ] **GET `/api/v1/profile`** — get own profile
- [ ] **PATCH `/api/v1/profile`** — update username/email/health condition/notification prefs
- [ ] **POST `/api/v1/profile/change-password`** — change password (bcrypt)
- [ ] **DELETE `/api/v1/profile`** — delete own account

---

## Phase 4: MQTT Ingestion Layer

- [ ] **MQTT client module** — `mqtt/client.py` — async MQTT client connecting to Mosquitto broker over TLS
- [ ] **Payload validation** — `mqtt/validator.py` — Pydantic/JSON schema validation for incoming sensor readings
- [ ] **Topic handler** — subscribe to `air/node/{id}/reading`, `air/node/{id}/status`
- [ ] **Reading ingestion flow** — receive MQTT message → validate → dispatch to Celery worker for processing
- [ ] **Status heartbeat handler** — update `last_seen` in nodes table on status messages
- [ ] **MQTT config publisher** — `mqtt/config.py` — publish config changes to `air/node/{id}/config`
- [ ] **TLS certificate handling** — client certificate auth for device connections
- [ ] **QoS management** — at-least-once delivery with retry logic

---

## Phase 5: Sensor Readings API

- [ ] **GET `/api/v1/readings/latest`** — latest reading per node (Redis-cached, TTL 60s)
- [ ] **GET `/api/v1/readings/history`** — time-bucketed historical readings (`from`, `to`, `node_id`, `bucket` params)
- [ ] **Redis caching layer** — `api/cache.py` — read-through cache pattern for readings
- [ ] **DTO schemas** — Pydantic models for request validation and response serialization
- [ ] **Rate limiting middleware** — Redis-based, 200 req/min per IP, `X-RateLimit-*` headers

---

## Phase 6: Tsukamoto Fuzzy Inference Engine

- [ ] **Membership functions** — `fuzzy/membership.py` — triangular/trapezoidal MFs for Temperature, Humidity, PM2.5
  - Temperature: Low (0–30), Medium (25–35–45), High (40–50)
  - Humidity: Dry (0–50), Humid (40–60–80), Wet (70–100)
  - PM2.5: Low (0–50), Medium (40–60–80), High (70–100+)
- [ ] **Fuzzy rules engine** — `fuzzy/rules.py` — full 27-rule rule base evaluation
- [ ] **Defuzzification** — `fuzzy/tsukamoto.py` — weighted-average defuzzification: `AQI_crisp = Σ(αᵢ × zᵢ) / Σ(αᵢ)`
- [ ] **Fuzzy inference pipeline** — compose membership → rules → defuzzification into single call
- [ ] **Unit tests** — `tests/test_fuzzy.py` — edge cases, boundary conditions, known output validation

---

## Phase 7: Celery Tasks (Async Processing)

### Fuzzy Inference & Enrichment
- [ ] **Process reading task** — consume raw sensor reading, run fuzzy inference, compute AQI, detect anomalies
- [ ] **AQI computation** — EPA AQI from PM2.5/PM10 (standard breakpoints)
- [ ] **Anomaly detection** — Z-score based anomaly flagging

### Scheduled Tasks (Celery Beat)
- [ ] **Hourly aggregation** — compute `hourly_agg` continuous aggregate refresh
- [ ] **Alert threshold check** — every 60s, check AQI against thresholds, create alert records on breach
- [ ] **Forecast model retraining** — hourly retrain of linear regression model
- [ ] **Data retention cleanup** — enforce 1-year retention policy

### Forecast
- [ ] **Forecast generation task** — linear regression/ARIMA for next-60-minute AQI prediction
- [ ] **GET `/api/v1/forecast`** — API endpoint for forecast data (Redis-cached, TTL 1h)

---

## Phase 8: Nodes API

- [ ] **GET `/api/v1/nodes`** — all registered nodes with metadata (Redis-cached, TTL 300s)
- [ ] **PATCH `/api/v1/nodes/:node_id`** — update node config (Admin only; pushes to device via MQTT)
- [ ] **Node registration endpoint** — POST `/api/v1/nodes` — register a new sensor node

---

## Phase 9: Alerts & WebSocket

### Alerts API
- [ ] **GET `/api/v1/alerts`** — list unacknowledged alerts (with `limit`, `offset`, `severity` filters)
- [ ] **PATCH `/api/v1/alerts/:alert_id/acknowledge`** — mark alert as acknowledged
- [ ] **Alert creation logic** — triggered by Celery Beat when AQI exceeds thresholds

### WebSocket
- [ ] **WebSocket connection manager** — `api/ws/manager.py` — track connected clients, handle lifecycle
- [ ] **Alert broadcasting** — bridge MQTT `air/alerts` topic to WebSocket clients
- [ ] **WebSocket auth** — authenticate WebSocket connections via JWT

---

## Phase 10: Admin Endpoints

- [ ] **GET `/api/v1/admin/health`** — system health check (MQTT, TimescaleDB, Redis, Celery worker/beat, DB & Redis size)
- [ ] **GET `/api/v1/admin/settings`** — view system settings (Admin only)
- [ ] **PATCH `/api/v1/admin/settings`** — update AQI thresholds, data retention, alert config
- [ ] **Admin middleware** — role-based access control for admin routes

---

## Phase 11: Export & Utilities

- [ ] **GET `/api/v1/export`** — CSV download of raw readings for a date range
- [ ] **CSV generator** — streaming CSV response for large exports
- [ ] **Health check endpoint** — basic `/health` or `/api/v1/admin/health` for monitoring

---

## Phase 12: Error Handling & Middleware

- [ ] **RFC 7807 Problem JSON** — standardized error responses (`application/problem+json`)
- [ ] **Global error handlers** — 400, 401, 403, 404, 422, 429, 500 error handlers
- [ ] **Request validation middleware** — Pydantic schema validation on all inputs
- [ ] **CORS configuration** — allow frontend origins, credentials support
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
| **M2: Auth** | Registration, login, JWT, profile management 🔜 | M1 |
| **M3: Ingestion** | MQTT consumer, payload validation, reading intake | M1 |
| **M4: Core Processing** | Fuzzy inference engine, Celery tasks, AQI computation | M2, M3 |
| **M5: API Layer** | Readings, nodes, alerts, forecast, export endpoints | M2, M4 |
| **M6: Real-time** | WebSocket alert broadcasting | M5 |
| **M7: Admin & Ops** | Admin endpoints, health monitoring | M5 |
| **M8: Hardening** | Tests, error handling, production deployment | M1–M7 |

---

*Generated from architecture spec in README.md*
