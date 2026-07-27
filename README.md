# Empyrean-V2-Backend

This repo is made for the Backend part of the Empyrean application (IoT Air Quality Mapping System) — a real-time, geospatially-aware environmental monitoring platform. It ingests sensor data over MQTT, runs it through a Tsukamoto Fuzzy Inference engine, stores it in TimescaleDB, and exposes it to the frontend via a versioned REST API.

> **Deployment status:** the physical hardware is still in development, so the current live deployment runs a single node (`ESP32-01`). The architecture below — node registration, per-node MQTT topics, `node_id`-partitioned TimescaleDB storage — already supports many concurrent nodes and needs no backend changes to scale up as more physical nodes come online; see NFR target of ≥ 50 nodes under Performance & Reliability Targets.

## Tech Stack

- **API Server:** Quart (async Flask) — REST endpoints, JWT auth, WebSocket push
- **Task Queue:** Celery + Redis — async fuzzy inference, anomaly detection, scheduled aggregation, alerting
- **Primary Database:** PostgreSQL 18 (TimescaleDB planned) — time-series storage, hypertable partitioning
- **Cache / Broker:** Redis — latest-reading cache, rate limiting, Celery broker
- **MQTT Broker:** Eclipse Mosquitto (TLS/MQTTS) — device ingestion, config push, alert broadcast
- **ML Engine:** Scikit-learn + Pandas — AQI forecasting (linear regression), Z-score anomaly detection
- **Auth:** JWT (RS256) — 15-min access tokens, 7-day refresh tokens
- **Process Management:** all services run as local processes (or systemd units) on a single host — see Running Locally / Production below
## Architecture Overview

The backend sits between the MQTT-publishing sensor nodes and the React frontend, and is composed of four cooperating services, all running on a single machine:

1. **MQTT Broker (Mosquitto)** — receives sensor payloads over TLS, authenticates devices via client certificates, and routes messages.
2. **Quart API Server** — the async MQTT consumer that validates incoming payloads, plus the REST/WebSocket layer the frontend talks to.
3. **Celery Worker + Beat** — runs the Tsukamoto fuzzy inference, computes AQI, flags anomalies, generates forecasts, and checks alert thresholds on a schedule.
4. **TimescaleDB + Redis** — durable time-series storage and a fast cache layer respectively.

### End-to-End Data Flow

| Step | Description |
|------|-------------|
| 1 | Sensor node publishes a JSON reading to MQTT topic `air/node/{id}/reading` every 30s over MQTTS. |
| 2 | Mosquitto authenticates the device certificate and routes the message to the Quart consumer. |
| 3 | Quart validates the payload against a JSON schema; malformed payloads are rejected and logged. |
| 4 | The valid reading is dispatched to a Celery worker via the Redis queue. |
| 5 | The worker runs Tsukamoto Fuzzy Inference on (Temperature, Humidity, PM2.5) to produce a 0–100 fuzzy score. |
| 6 | The worker computes the EPA AQI from PM2.5/PM10 and runs a Z-score anomaly check. |
| 7 | The enriched record is inserted into the `sensor_readings` table in PostgreSQL (will become a TimescaleDB hypertable later). |
| 8 | The `readings:latest:{node_id}` Redis key is updated (TTL 60s), invalidating the stale cache. |
| 9 | Celery Beat checks AQI thresholds every 60s; on breach, an alert row is written and pushed to connected clients over WebSocket. |
| 10 | The frontend polls `GET /api/v1/readings/latest` every 5s, hitting the Redis cache for a sub-10ms response. Note: the response is a flat `{ "nodes": [...] }` array with `lat`/`lon` fields, not GeoJSON — the frontend maps this into GeoJSON client-side for Leaflet if needed. |

**Target:** end-to-end latency from sensor reading to dashboard visibility is **< 2 seconds**.

## Component Responsibilities

| Component | Technology | Responsibility |
|---|---|---|
| MQTT Broker | Eclipse Mosquitto | Message routing, TLS termination, device authentication, QoS management |
| API Server | Quart (async Flask) | REST endpoints, JWT auth, WebSocket push, request validation |
| Task Queue | Celery + Redis | Async fuzzy inference, anomaly detection, scheduled aggregation, alerts |
| Primary DB | TimescaleDB (PostgreSQL) | Time-series storage, hypertable partitioning, continuous aggregates |
| Cache | Redis | Latest-reading cache, rate limiting, Celery broker |
| ML Engine | Scikit-learn + Pandas | ARIMA/linear regression forecasting, Z-score anomaly detection, preprocessing |

## Project Structure

```
backend/
├── api/                    # Quart route handlers (auth, readings, nodes, alerts, forecast, export, profile, admin)
├── fuzzy/                  # Tsukamoto fuzzy inference engine
├── tasks/                  # Celery worker + beat task definitions
├── models/
│   ├── __init__.py         # Re-exports all models + base utilities
│   ├── base.py             # SQLAlchemy engine, session factories, get_db(), error handling, retry logic
│   ├── user.py             # User model (both admin & regular users)
│   ├── refresh_token.py    # RefreshToken model (server-side JWT storage, revocation, rotation)
│   ├── node.py             # Node model (ESP32 sensor devices, location tracking)
│   ├── reading.py          # SensorReading model (core time-series data, composite PK)
│   ├── aggregate.py        # HourlyAgg model (pre-computed hourly summaries)
│   ├── alert.py            # Alert model (threshold-breach notifications)
│   └── setting.py          # SystemSetting model (configurable system knobs)
├── mqtt/                   # MQTT consumer & payload validation
├── ws/                     # WebSocket alert broadcasting
├── migrations/
│   ├── env.py              # Alembic environment (wired to models)
│   └── versions/
│       └── xxx_initial_schema.py   # Initial schema migration (7 tables + indexes)
├── tests/                  # pytest suite
├── config/__init__.py      # App configuration (env-based, Dev/Prod)
├── app.py                  # Quart application factory
├── celery_app.py           # Celery application instance
├── seed.py                 # Dev seed script (admin user, defaults, sample node)
├── check_health.py         # Health check script (validates entire stack)
├── requirements.txt
├── .env                    # Local credentials (gitignored)
└── .env.example            # Template env vars (safe to commit)
```

## Tsukamoto Fuzzy Inference Engine

The AQI fuzzy engine consumes three input variables and outputs a crisp 0–100 score via weighted-average defuzzification: `AQI_crisp = Σ(αᵢ × zᵢ) / Σ(αᵢ)`, where `αᵢ` is each rule's firing strength (min of antecedent memberships) and `zᵢ` is the crisp output from the consequent's monotonic function.

**Input variables:**

| Variable | Range | Fuzzy Terms |
|---|---|---|
| Temperature (T) | 0–50 °C | Low (0–30), Medium (25–35–45), High (40–50) |
| Humidity (H) | 0–100 % | Dry (0–50), Humid (40–60–80), Wet (70–100) |
| PM2.5 Pollution (P) | 0–500 µg/m³ | Low (0–50), Medium (40–60–80), High (70–100+) |

**Rule base (sample — 6 of 27 possible combinations):**

| Rule | Condition | Output |
|---|---|---|
| R1 | T=Medium AND P=Medium AND H=Humid | AQI = Medium |
| R2 | T=High AND P=High | AQI = Bad |
| R3 | T=Low AND P=Low AND H=Wet | AQI = Good |
| R4 | T=High AND P=Medium | AQI = Medium |
| R5 | T=Low AND P=High | AQI = Bad |
| R6 | T=Medium AND P=Low | AQI = Good |

> With 3 variables × 3 terms there are up to 27 possible rule combinations. The table above is illustrative only — the full rule base needs to be finalized in `backend/fuzzy/tsukamoto.py` before the engine is actually implementable; treat this as an open task, not a spec.

## REST API

All endpoints are prefixed with `/api/v1/`. Auth uses JWT RS256 Bearer tokens (except login/refresh). Errors follow **RFC 7807 Problem JSON** (`Content-Type: application/problem+json`).

### Authentication
| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/auth/login` | POST | No | Returns `access_token`, `refresh_token`, `expires_in`, `role` |
| `/auth/refresh` | POST | No | Exchanges a refresh token for a new access token |

### Sensor Readings
| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/readings/latest` | GET | Yes | Latest reading per node. Redis-cached, TTL 60s. Polled every 5s by the dashboard. |
| `/readings/history` | GET | Yes | Time-bucketed historical readings (`from`, `to`, `node_id`, `bucket`) via `time_bucket()` / `hourly_agg` |

### Nodes
| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/nodes` | GET | Yes | All registered nodes with metadata. Redis-cached, TTL 300s. |
| `/nodes/:node_id` | PATCH | Admin | Update name, location, reading interval, or active status (pushes config to device via MQTT) |

### Alerts
| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/alerts` | GET | Yes | Unacknowledged threshold-breach alerts (`limit`, `offset`, `severity`) |
| `/alerts/:alert_id/acknowledge` | PATCH | Yes | Marks an alert acknowledged |

### Forecast
| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/forecast` | GET | Yes | Next-60-minute AQI prediction (linear regression, retrained hourly, cached 1h) |

### Export
| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/export` | GET | Yes | CSV download of raw readings for a date range |

### Profile
| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/profile` | GET | Yes | Get own profile |
| `/profile` | PATCH | Yes | Update username/email/health condition/notification prefs |
| `/profile/change-password` | POST | Yes | Change password |
| `/profile` | DELETE | Yes | Delete own account |

### Admin
| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/admin/health` | GET | Admin | Status of MQTT broker, TimescaleDB, Redis, Celery worker/beat, DB & Redis size |
| `/admin/settings` | GET/PATCH | Admin | AQI thresholds, data retention, alert email, alerts enabled flag |

Full request/response field tables are in `api_documentation.pdf`.

## MQTT Topic Schema

| Topic | Direction | Payload |
|---|---|---|
| `air/node/{id}/reading` | Device → Broker → Backend | Full sensor reading JSON |
| `air/node/{id}/status` | Device → Broker | Heartbeat: `{ online, battery_v, firmware }` |
| `air/node/{id}/config` | Backend → Device | Remote config: `{ interval_s, fuzzy_enabled }` |
| `air/alerts` | Backend → Subscribers | Alert broadcast: `{ node_id, aqi, category, timestamp }`, bridged to the frontend over WebSocket |

QoS level 1 (at least once) is used for all device publishes.

## Database Schema

Seven tables implemented via SQLAlchemy 2.0 + Alembic migrations:

### `users` — who logs in
| Column | Type | Notes |
|--------|------|-------|
| `id` | `INTEGER PK` | Auto-increment identity |
| `username` | `VARCHAR(50) UNIQUE` | Login name |
| `email` | `VARCHAR(255) UNIQUE` | Notifications / password resets |
| `password_hash` | `VARCHAR(255)` | bcrypt hash |
| `role` | `VARCHAR(20)` | `'admin'` or `'user'` |
| `notification_prefs` | `JSONB` | Flexible prefs (e.g. email on critical) |
| `is_active` | `BOOLEAN` | Soft-disable without deleting |
| `last_login_at` | `TIMESTAMPTZ` | Audit trail |
| `created_at` | `TIMESTAMPTZ` | Auto-set |
| `updated_at` | `TIMESTAMPTZ` | Auto-updated |

### `refresh_tokens` — session management
Server-side storage enabling logout (set `revoked = True`) and refresh-token rotation.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `INTEGER PK` | |
| `user_id` | `INTEGER FK → users` | ON DELETE CASCADE |
| `token_hash` | `VARCHAR(255)` | Hashed token (never store raw) |
| `expires_at` | `TIMESTAMPTZ` | Matches JWT claim |
| `created_at` | `TIMESTAMPTZ` | |
| `revoked` | `BOOLEAN` | TRUE on logout/rotation |

Indexed on `(user_id)` and `(token_hash)`.

### `nodes` — sensor devices
| Column | Type | Notes |
|--------|------|-------|
| `node_id` | `VARCHAR(50) PK` | Device ID (e.g. `ESP32-01`) |
| `name` | `VARCHAR(100)` | Human-friendly label |
| `location_name` | `VARCHAR(200)` | Text description |
| `lat` / `lon` | `DOUBLE PRECISION` | For map display |
| `firmware_version` | `VARCHAR(50)` | Debugging aid |
| `reading_interval` | `INTEGER` | Seconds between readings (default 30) |
| `is_active` | `BOOLEAN` | Soft-retire a node |
| `registered_at` | `TIMESTAMPTZ` | |
| `last_seen` | `TIMESTAMPTZ` | Updated by heartbeat |

### `sensor_readings` — the core data
**Note:** Currently a regular PostgreSQL table. Will be converted to a TimescaleDB hypertable (partitioned by `time`, 7-day chunks) once the extension is installed.

| Column | Type | Range | Notes |
|--------|------|-------|-------|
| `time` | `TIMESTAMPTZ` (PK) | — | Partition column |
| `node_id` | `VARCHAR(50)` (PK, FK → nodes) | — | Which node |
| `temperature` | `REAL` | 0–50 °C | |
| `humidity` | `REAL` | 0–100% | |
| `pressure` | `REAL` | 900–1100 hPa | |
| `voc_ohm` | `REAL` | — | BME680 raw resistance |
| `mq135_ppm` | `REAL` | 0–1000+ | MQ135 PPM |
| `pm1` / `pm25` / `pm10` | `REAL` | Various | Particulate matter |
| `battery_v` | `REAL` | 0–5 V | Node health |
| `fuzzy_score` | `REAL` | 0–100 | Tsukamoto fuzzy score |
| `aqi` | `SMALLINT` | 0–500+ | EPA AQI from PM2.5/PM10 |
| `aqi_category` | `VARCHAR(40)` | — | "Good", "Moderate", etc. |
| `is_anomaly` | `BOOLEAN` | — | Z-score anomaly flag |

Performance indexes: `(node_id, time DESC)` and `(time DESC)`.

### `hourly_agg` — pre-computed summaries
Currently a regular materialized view (will become a TimescaleDB continuous aggregate later).

| Column | Type | Notes |
|--------|------|-------|
| `bucket` | `TIMESTAMPTZ` (PK) | Hour start |
| `node_id` | `VARCHAR(50)` (PK, FK → nodes) | |
| `avg_temperature` / `avg_humidity` / `avg_pm25` / `avg_pm10` | `REAL` | Hourly averages |
| `max_aqi` / `min_aqi` / `avg_aqi` | `SMALLINT` / `REAL` | AQI stats |
| `anomaly_count` | `INTEGER` | Anomalies that hour |
| `reading_count` | `INTEGER` | Total readings |

### `alerts` — threshold-breach notifications
| Column | Type | Notes |
|--------|------|-------|
| `alert_id` | `INTEGER PK` | Auto-increment |
| `node_id` | `VARCHAR(50) FK → nodes` | ON DELETE CASCADE |
| `parameter` | `VARCHAR(50)` | e.g. `'pm25'`, `'aqi'` |
| `value` / `threshold` | `REAL` | Actual vs. limit |
| `severity` | `VARCHAR(20)` | `'warning'` or `'critical'` |
| `message` | `TEXT` | Human-readable |
| `triggered_at` | `TIMESTAMPTZ` | |
| `acknowledged_at` | `TIMESTAMPTZ` | NULL = unacknowledged |
| `acknowledged_by` | `INTEGER FK → users` | ON DELETE SET NULL |

### `system_settings` — configurable knobs
| Column | Type | Notes |
|--------|------|-------|
| `key` | `VARCHAR(100) PK` | e.g. `'aqi_warning_threshold'` |
| `value` | `TEXT` | Stored as text, cast when needed |
| `description` | `VARCHAR(255)` | Human explanation |
| `updated_at` | `TIMESTAMPTZ` | |
| `updated_by` | `INTEGER FK → users` | ON DELETE SET NULL |

Default settings seeded: `aqi_warning_threshold=100`, `aqi_critical_threshold=150`, `alerts_enabled=true`

## Redis Key Schema

| Key Pattern | TTL | Value |
|---|---|---|
| `readings:latest:{node_id}` | 60s | Latest enriched reading (JSON) |
| `nodes:all` | 300s | All node metadata (JSON array) |
| `alerts:unacked` | 30s | Unacknowledged alerts (JSON array) |
| `ratelimit:{ip}:{minute}` | 60s | Request count (int) |
| `celery:forecast:{node_id}` | 3600s | AQI forecast array (JSON) |

TTLs are tuned per data volatility, not a single blanket value: live readings never go stale beyond 60s, while less time-sensitive data (node metadata, forecasts) uses a longer TTL to cut down on recomputation.

## Security

- JWT RS256, 15-min access / 7-day refresh token expiry
- MQTT over TLS (MQTTS, port 8883) — no plaintext MQTT
- REST API over HTTPS only; HTTP redirects to HTTPS
- Passwords hashed with bcrypt (cost factor ≥ 12)
- API rate-limited to 200 requests/minute per IP via Redis (`X-RateLimit-*` headers, `429` + `Retry-After` on breach)
- All inputs validated with Pydantic schemas
- Devices authenticate to the MQTT broker with unique client certificates
- DB credentials passed via environment variables — never hardcoded

## Performance & Reliability Targets

| Metric | Target |
|---|---|
| Sensor → dashboard end-to-end latency | < 2s |
| MQTT broker acknowledgement time | < 300ms |
| REST API 95th-percentile response | < 200ms |
| Concurrent MQTT messages handled | ≥ 50 nodes |
| API throughput | ≥ 100 RPS |
| 30-day time-range aggregate query | < 100ms (TimescaleDB) |
| System uptime | ≥ 99% |

## Environment Variables

| Variable | Example | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:root@localhost:5432/Empyren` | PostgreSQL connection string (use `.env`, NOT tracked in git) |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `MQTT_BROKER_HOST` | `localhost` | MQTT broker hostname |
| `MQTT_BROKER_PORT` | `8883` | MQTT TLS port |
| `JWT_SECRET` | `<256-bit random>` | JWT signing secret |
| `JWT_ALGORITHM` | `RS256` | JWT algorithm |
| `AQI_WARNING_THRESHOLD` | `100` | AQI value that triggers a warning alert |
| `AQI_CRITICAL_THRESHOLD` | `150` | AQI value that triggers a critical alert |

## Services (single-system deployment)

All services run as local processes on one host — no containers. Use a process manager (`systemd`, `supervisord`, or `pm2`) in production to keep them running and restart on failure.

| Service | Run as | Port | Notes |
|---|---|---|---|
| `quart-api` | `hypercorn app:app` (or `quart run`) | 8000 | Quart async API server |
| `celery-worker` | `celery -A tasks worker` | — | Fuzzy inference + ML tasks |
| `celery-beat` | `celery -A tasks beat` | — | Scheduled aggregation + alert checks |
| `mosquitto` | native install / systemd service | 8883 (MQTTS) | MQTT broker with TLS |
| `timescaledb` | native PostgreSQL + TimescaleDB extension | 5432 | Primary database |
| `redis` | native install / systemd service | 6379 | Cache + Celery broker |

## Getting Started

### Prerequisites
- **Python** 3.12+
- **PostgreSQL** 17+ (18 recommended)
- **Redis** (for Celery broker + cache)

### Setup

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd Empyrean-V2-Backend

# 2. Create and activate virtual environment
python -m venv venv
source venv/Scripts/activate    # Windows Git Bash
# or: venv\Scripts\activate     # Windows cmd
# or: source venv/bin/activate  # Linux/Mac

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
#    Copy .env.example → .env and fill in your credentials:
#    DATABASE_URL=postgresql://postgres:your_password@localhost:5432/Empyren
cp .env.example .env
# Edit .env with your actual database credentials

# 5. Create the database (if it doesn't exist)
psql -U postgres -c "CREATE DATABASE \"Empyren\";"

# 6. Run migrations
alembic upgrade head

# 7. Seed initial data (admin user, defaults, sample node)
python seed.py

# 8. Verify everything is working
python check_health.py

# 9. Start the API server
hypercorn app:app --bind 0.0.0.0:8000

# (in separate terminals) Start Celery worker and beat
celery -A tasks worker --loglevel=info
celery -A tasks beat --loglevel=info
```

The API will be available at `http://localhost:8000/api/v1/`.

### Health Check

Run `python check_health.py` at any time to verify:
- Python environment and model imports
- PostgreSQL connection and database version
- All 7 tables and required indexes exist
- Alembic migration is applied
- Seed data is present (admin user, default settings, sample node)
- Quart app factory loads without errors

### Database Migrations

```bash
# Create a new migration after model changes
alembic revision --autogenerate -m "description_of_change"

# Apply pending migrations
alembic upgrade head

# Roll back one step
alembic downgrade -1
```

### Seed Data

The `seed.py` script is idempotent — running it multiple times is safe:

```
Created admin user: admin / admin123
Created setting: aqi_warning_threshold = 100
Created setting: aqi_critical_threshold = 150
Created setting: alerts_enabled = true
Created sample node: ESP32-01
```

Default admin credentials: `admin` / `admin123` (change in production).

### Production

Run the same processes under `systemd` unit files (or `supervisord`) so they restart automatically and start on boot. A minimal `quart-api.service` example:

```ini
[Unit]
Description=Empyrean Quart API
After=network.target postgresql.service redis-server.service mosquitto.service

[Service]
WorkingDirectory=/opt/empyrean-backend
EnvironmentFile=/opt/empyrean-backend/.env
ExecStart=/opt/empyrean-backend/venv/bin/hypercorn app:app --bind 0.0.0.0:8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Mirror this pattern for `celery-worker.service` and `celery-beat.service`. Put TLS termination for the REST API (HTTPS) in front via nginx or Caddy on the same host.

## Scalability & Maintainability Notes

- All services run as independent local processes on a single host, managed by `systemd`/`supervisord`
- The Quart API is stateless at the process level; if load ever requires more than one host, it can be scaled behind a load balancer without code changes
- Redis TTLs are tuned per data volatility (see Redis Key Schema) — live readings never stale beyond 60s, other caches longer by design
- Environment-specific config lives entirely in `.env` files — no environment branching in code

## Connecting to the Frontend Repo

The frontend (`Empyrean-V2-Frontend` — React + Leaflet + Recharts) lives in a separate repo and talks to this backend purely over HTTP/WebSocket. There's no shared codebase — just a URL and an auth contract both sides agree on.

### 1. CORS

The frontend dev server and this API run on different origins/ports, so the backend must explicitly allow the frontend's origin. In the Quart app:

```python
from quart_cors import cors

app = cors(
    app,
    allow_origin=["http://localhost:3000", "https://<your-frontend-domain>"],
    allow_credentials=True,
)
```

Add every environment the frontend is served from (local dev port, staging, production domain).

### 2. Base URL the frontend should point at

The frontend should never hardcode the API host — it reads it from an env var:

```
# frontend/.env
VITE_API_BASE_URL=http://localhost:8000/api/v1
VITE_WS_URL=ws://localhost:8000/ws/alerts
```

In production these become `https://<your-domain>/api/v1` and `wss://<your-domain>/ws/alerts`. Every fetch/axios call in the frontend should build off `VITE_API_BASE_URL`, matching the routes documented above (e.g. `${VITE_API_BASE_URL}/readings/latest`).

### 3. Auth handshake

1. Frontend POSTs `username`/`password` to `/auth/login`, receives `access_token`, `refresh_token`, `expires_in`, and `role`.
2. Frontend stores tokens in memory (not localStorage, per the security requirements) and attaches `Authorization: Bearer <access_token>` to every subsequent request.
3. `role` (`analyst` / `admin`) drives which routes/nav items the frontend renders — admin-only pages should call `/admin/*` and `/nodes` PATCH endpoints only when `role === "admin"`.
4. When a request comes back `401` with an expired-token detail, the frontend should silently call `/auth/refresh` with the stored `refresh_token`, get a new `access_token`, and retry the original request once. If refresh also fails (`401`), force logout and redirect to `/`.

### 4. Live data contract

- **Polling:** the dashboard map polls `GET /readings/latest` every 5 seconds and `GET /nodes` less frequently (it's cached 300s server-side) — no backend change needed to adjust this, it's purely a frontend interval.
- **Push:** the frontend opens a WebSocket to `VITE_WS_URL` to receive `air/alerts` broadcasts in real time (threshold-breach toasts) instead of polling `/alerts`. The backend bridges the MQTT `air/alerts` topic onto this WebSocket.
- **Rate limits:** the frontend should respect `X-RateLimit-Remaining`/`Retry-After` headers and back off polling if it starts hitting `429`.

### 5. Local dev — running both together

Since they're separate repos, run each in its own terminal (or use `concurrently` from a root script):

```bash
# terminal 1 — backend (see "Running Locally" above)
cd Empyrean-V2-Backend
source venv/bin/activate
hypercorn app:app --bind 0.0.0.0:8000
# plus celery worker/beat in their own terminals

# terminal 2 — frontend
cd Empyrean-V2-Frontend && npm run dev
```

Confirm connectivity with `curl http://localhost:8000/api/v1/admin/health` (once authenticated) or by checking the browser network tab for successful `/readings/latest` calls with no CORS errors.

### 6. Keeping the contract in sync

Since backend and frontend evolve independently, treat `api_documentation.pdf` (or an OpenAPI spec generated from it) as the source of truth for field names and shapes. Any breaking change to a response schema should be released under `/api/v2/` per the versioning policy, so the existing frontend deployment keeps working against `/api/v1/` until it's updated.

## Hardware / Firmware (reference only)

The backend consumes data published by ESP32 nodes (MicroPython) fitted with BME680, MQ135, PMS5003, and NEO-6M GPS sensors, which publish JSON readings to `air/node/{id}/reading` over MQTTS every 30 seconds. Firmware details, wiring, and on-device pre-classification are out of scope for this repo — see the hardware/firmware repo for that implementation.