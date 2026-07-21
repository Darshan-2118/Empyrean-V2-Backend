# Empyrean-V2-Backend

This repo is made for the Backend part of the Empyrean application (IoT Air Quality Mapping System) — a real-time, geospatially-aware environmental monitoring platform. It ingests sensor data over MQTT, runs it through a Tsukamoto Fuzzy Inference engine, stores it in TimescaleDB, and exposes it to the frontend via a versioned REST API.

> **Deployment status:** the physical hardware is still in development, so the current live deployment runs a single node (`ESP32-01`). The architecture below — node registration, per-node MQTT topics, `node_id`-partitioned TimescaleDB storage — already supports many concurrent nodes and needs no backend changes to scale up as more physical nodes come online; see NFR target of ≥ 50 nodes under Performance & Reliability Targets.

## Tech Stack

- **API Server:** Quart (async Flask) — REST endpoints, JWT auth, WebSocket push
- **Task Queue:** Celery + Redis — async fuzzy inference, anomaly detection, scheduled aggregation, alerting
- **Primary Database:** TimescaleDB (PostgreSQL) — time-series storage, hypertable partitioning, continuous aggregates
- **Cache / Broker:** Redis — latest-reading cache, rate limiting, Celery broker
- **MQTT Broker:** Eclipse Mosquitto (TLS/MQTTS) — device ingestion, config push, alert broadcast
- **ML Engine:** Scikit-learn + Pandas — AQI forecasting (linear regression), Z-score anomaly detection
- **Auth:** JWT (RS256) — 15-min access tokens, 7-day refresh tokens
- **Process Management:** all services run as local processes (or systemd units) on a single host — see Running Locally / Production below
- **CI/CD:** GitHub Actions

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
| 7 | The enriched record is inserted into the `sensor_readings` hypertable in TimescaleDB. |
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

## Project Structure (suggested)

```
backend/
├── api/                # Quart route handlers (auth, readings, nodes, alerts, forecast, export, profile, admin)
├── fuzzy/
│   └── tsukamoto.py     # Tsukamoto fuzzy inference engine implementation
├── tasks/               # Celery worker + beat task definitions
├── models/               # DB models / schemas
├── mqtt/                # MQTT consumer & payload validation
├── ws/                   # WebSocket alert broadcasting
├── migrations/           # DB migrations
├── tests/                 # pytest suite
├── requirements.txt
└── .env.example
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

### TimescaleDB — `sensor_readings` (hypertable, 7-day chunks)
Columns: `time`, `node_id`, `lat`, `lon`, `temperature`, `humidity`, `pressure`, `voc_ohm`, `mq135_ppm`, `pm1`, `pm25`, `pm10`, `fuzzy_score`, `aqi`, `aqi_category`, `is_anomaly`, `battery_v`.

- Hypertable creation: `create_hypertable('sensor_readings', 'time', chunk_time_interval => INTERVAL '7 days')`
- Compression enabled after 30 days via `add_compression_policy`
- Retention: raw readings auto-dropped after 1 year; aggregates retained indefinitely
- `aqi_category` is `VARCHAR(40)` (not `VARCHAR(20)`) to safely fit the longest EPA category name, "Unhealthy for Sensitive Groups" (30 chars). The 6 standard categories are: Good, Moderate, Unhealthy for Sensitive Groups, Unhealthy, Very Unhealthy, Hazardous.

### `hourly_agg` (Continuous Aggregate)
Refreshes hourly. Stores `time_bucket('1 hour')`, `node_id`, `avg_temp`, `avg_humidity`, `avg_pm25`, `avg_pm10`, `max_aqi`, `min_aqi`, `avg_aqi`, `reading_count`. Used for large-range history queries.

### `nodes`
`node_id` (PK), `name`, `location_name`, `firmware_version`, `registered_at`, `last_seen`, `active`.

### `alerts`
`alert_id` (PK, UUID), `node_id` (FK), `parameter`, `value`, `threshold`, `severity`, `triggered_at`, `acknowledged_at`, `acknowledged_by`.

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
| `DATABASE_URL` | `postgresql://user:pass@localhost:5432/airquality` | TimescaleDB connection string |
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

## Running Locally

```bash
# copy and fill in environment variables
cp .env.example .env

# create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# install dependencies
pip install -r requirements.txt

# make sure Redis, TimescaleDB (PostgreSQL), and Mosquitto are installed and running locally
# (e.g. `sudo systemctl start redis-server postgresql mosquitto` on Debian/Ubuntu)

# run DB migrations
alembic upgrade head   # or your migration tool of choice

# start the API server
hypercorn app:app --bind 0.0.0.0:8000

# in separate terminals, start the Celery worker and beat scheduler
celery -A tasks worker --loglevel=info
celery -A tasks beat --loglevel=info
```

The API will be available at `http://localhost:8000/api/v1/`.

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

## CI/CD (GitHub Actions)

1. On push to `main`: run `pytest` for fuzzy inference, API routes, and DB queries.
2. On passing tests: build a deployable artifact (e.g. a tarball or wheel) — no container image.
3. On tagged release: deploy to the production host via SSH (e.g. `rsync` the artifact + `systemctl restart` the services above).
4. Secrets (`DB_URL`, `JWT_SECRET`, `MQTT_CERT`) are managed via GitHub Secrets.

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