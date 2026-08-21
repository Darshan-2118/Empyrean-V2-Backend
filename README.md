# Empyrean V2 — Air Quality Monitoring & Analytics Platform

[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![Framework](https://img.shields.io/badge/framework-Quart%20(ASGI)-brightgreen.svg)](https://pgjones.gitlab.io/quart/)
[![Database](https://img.shields.io/badge/database-PostgreSQL%20%2B%20TimescaleDB-blue.svg)](https://www.timescale.com/)
[![Broker](https://img.shields.io/badge/broker-Redis%20%2B%20MQTT-orange.svg)](https://redis.io/)
[![Async](https://img.shields.io/badge/tasks-Celery%20%2B%20Beat-green.svg)](https://docs.celeryq.dev/)

Empyrean V2 is a real-time air quality ingestion, analysis, alerting, and forecasting backend platform. Built on **Quart (async Python/ASGI)**, **Celery**, **PostgreSQL with TimescaleDB**, **Redis**, and **MQTT**, it ingests sensor telemetry from IoT nodes, processes fuzzy logic AQI ratings and anomaly detection, triggers instant alerts over WebSockets, and delivers time-series analytics and forecasting.

---

## 🏗️ Architecture & Core Components

```
                +-------------------------+
                |   IoT Sensor Nodes      |
                +------------+------------+
                             | (MQTT telemetry)
                             v
                +-------------------------+
                |    MQTT Broker (TLS)    |
                +------------+------------+
                             |
                             v
+-----------------------------------------------------------+
|  Empyrean Backend Services                                |
|                                                           |
|  [MQTT Client Lifecycle]                                  |
|         │ (validates schema, extracts node_id)            |
|         ▼                                                 |
|  [Celery Task Queue: Redis Broker]                        |
|         │                                                 |
|         ├──▶ [process_reading] ──▶ Anomaly Detection      |
|         │                      ──▶ TimescaleDB Storage    |
|         │                      ──▶ AQI Calculation        |
|         │                      ──▶ Threshold & Alerts     |
|         │                                                 |
|         └──▶ [Celery Beat Schedulers]                     |
|                ├── Node offline heartbeats                |
|                ├── Daily statistical aggregations         |
|                ├── Data retention cleanup                 |
|                └── Hourly AQI forecast updates            |
|                                                           |
|  [Quart ASGI HTTP & WebSocket Server] (Hypercorn)         |
|         ├── REST API: /api/v1/{auth, readings, nodes, ...}|
|         ├── Live WebSocket: /ws/alerts, /ws/live          |
|         ├── Prometheus Metrics: /metrics                  |
|         └── System Health: /admin/health                  |
|                                                           |
|  [Persistence & Cache Layer]                              |
|         ├── PostgreSQL + TimescaleDB (time-series)        |
|         └── Redis (rate limiting, cache, celery broker)   |
+-----------------------------------------------------------+
```

---

## 📋 Prerequisites

| Component | Minimum Version | Notes |
|-----------|-----------------|-------|
| **Python** | `3.10` – `3.12` | Required runtime environment |
| **PostgreSQL** | `14+` | Primary relational and time-series database |
| **TimescaleDB** | `2.x+` (extension) | Required for `time_bucket()` time-series aggregations |
| **Redis** | `6.0+` | Celery message broker, query caching, rate limiting |
| **MQTT Broker** | Mosquitto / EMQX / HiveMQ | For IoT sensor telemetry ingestion |

---

## 🚀 Quick Start (Local Development)

### 1. Clone & Set Up Python Environment

```bash
# Clone repository
git clone https://github.com/Darshan-2118/Empyrean-V2-Backend.git
cd Empyrean-V2-Backend

# Windows PowerShell / CMD
python -m venv .venv
.\.venv\Scripts\activate

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables & Secrets

```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

Generate 256-bit cryptographically secure secrets and write them to your `.env` automatically:
```bash
python scripts/generate_secrets.py --write-env
```

> 🔒 **Security Notice:** The application enforces strict fail-fast validation in `config/__init__.py`. It will reject development placeholders (such as `dev-secret-key`, `dev-jwt-secret`, or `change-me-*`), keys shorter than 32 bytes, or low-entropy secrets. Running `scripts/generate_secrets.py` ensures your secrets comply with production constraints.

Update your `.env` file with your `DATABASE_URL`, `REDIS_URL`, and `MQTT_BROKER_HOST`.

### 4. Database Setup & Migrations

Ensure PostgreSQL with TimescaleDB is running, then apply database migrations:
```bash
alembic upgrade head
```

*(Optional)* Seed sample nodes, admin accounts, and initial configurations:
```bash
python scripts/seed.py
```

### 5. Running the Stack

#### Option A: One-Click Launch (Windows)
```bash
scripts\dev-up.bat
```
*(Launches Celery worker, Celery beat scheduler, and the Hypercorn ASGI server in separate console windows).*

To shut down:
```bash
scripts\dev-down.bat
```

#### Option B: Manual Service Launch

**Terminal 1 — Redis Server (if using WSL on Windows):**
```bash
wsl redis-server --daemonize yes
```

**Terminal 2 — Celery Worker:**
```bash
celery -A celery_app.celery_app worker --loglevel=info
```

**Terminal 3 — Celery Beat Scheduler:**
```bash
celery -A celery_app.celery_app beat --loglevel=info
```

**Terminal 4 — Quart ASGI API Server (Hypercorn):**
```bash
hypercorn "app:create_app()" --bind 0.0.0.0:8000 --reload
```

---

## 🧪 Testing & Verification

Run the comprehensive project verification script:
```bash
# Full verification (Database connectivity, migrations, health check, test suite)
python scripts/verify.py

# Quick verification (Skip unit tests)
python scripts/verify.py --quick
```

Run pytest directly:
```bash
pytest
```

Verify service health via HTTP:
```bash
curl http://localhost:8000/admin/health
```

---

## 📡 API Overview

| Group | Endpoint | Method | Description |
|-------|----------|--------|-------------|
| **Auth** | `/api/v1/auth/register` | `POST` | Register a new user |
| | `/api/v1/auth/login` | `POST` | Authenticate and obtain JWT token pair |
| | `/api/v1/auth/refresh` | `POST` | Rotate and issue fresh access/refresh tokens |
| | `/api/v1/auth/logout` | `POST` | Invalidate active refresh token |
| | `/api/v1/auth/me` | `GET` / `PATCH` | Current user profile |
| **Readings** | `/api/v1/readings/latest` | `GET` | Latest telemetry across nodes |
| | `/api/v1/readings/history` | `GET` | Aggregated time-series history (`time_bucket`) |
| | `/api/v1/readings/export` | `GET` | Export sensor telemetry (CSV / JSON) |
| **Nodes** | `/api/v1/nodes` | `GET` / `POST` | List and register IoT sensor nodes |
| | `/api/v1/nodes/<node_id>` | `GET` / `PATCH` | Node detail and configuration updates |
| **Alerts** | `/api/v1/alerts` | `GET` | List active & historic alerts |
| | `/api/v1/alerts/<id>/ack` | `POST` | Acknowledge active alert |
| **Forecast** | `/api/v1/forecast/<node_id>` | `GET` | Short-term AQI trend forecast |
| **System** | `/admin/health` | `GET` | Component health diagnostics |
| | `/metrics` | `GET` | Prometheus instrumentation metrics |
| **WebSockets** | `/ws/alerts` | `WS` | Real-time threshold breach notifications |
| | `/ws/live` | `WS` | Live sensor telemetry stream |

---

## 🚢 Production Deployment

The `deploy/` directory provides complete automation for bare-metal / VPS production environments:

### 1. Architecture Requirements in Production
- **Reverse Proxy**: Nginx with SSL/TLS termination, HTTP $\rightarrow$ HTTPS redirect, and WebSocket proxying (`deploy/nginx.conf`).
- **Process Supervision**: Systemd units for the API, Celery worker, and beat scheduler:
  - `deploy/quart-api.service` — Hypercorn running `app:create_app()` with 2 workers
  - `deploy/celery-worker.service` — Celery worker task consumer
  - `deploy/celery-beat.service` — Celery beat cron scheduler
- **Database**: PostgreSQL with TimescaleDB extension enabled (`CREATE EXTENSION IF NOT EXISTS timescaledb;`).
- **Telemetry Security**: MQTT Broker with TLS mutual authentication (`MQTT_USE_TLS=true`). Client certificates and CA certs are loaded from `/opt/empyrean/certs/` (`ca.crt`, `client.crt`, `client.key`) with fail-closed security.

### 2. Automated Deployment Pipeline

Export your target server details and execute the deployment script:
```bash
export SERVER_HOST="your-server-ip-or-domain"
export SERVER_USER="empyrean"
export DOMAIN="api.yourdomain.com"     # Optional: automatically templates Nginx config

bash deploy/deploy.sh
```

`deploy/deploy.sh` automatically:
1. Syncs project code via `rsync` (excluding `.git`, `venv`, and local secrets).
2. Provisions the virtualenv and updates dependencies.
3. Installs and enables systemd service units.
4. Generates/preserves `/opt/empyrean/.env` with strict `600` permissions.
5. Templates and enables Nginx configuration (with domain and SSL paths).
6. Runs database migrations (`alembic upgrade head`).

---

## 💡 Deployment Readiness Checklist

Before going live, verify the following configuration checklist:

- [x] **TimescaleDB Installed**: PostgreSQL server has `timescaledb` extension installed and enabled on the target database.
- [ ] **Cryptographic Secrets Generated**: Run `python scripts/generate_secrets.py --write-env` to ensure `SECRET_KEY` and `JWT_SECRET` are high-entropy 256-bit keys.
- [ ] **Production MQTT Broker & TLS**: When `MQTT_USE_TLS=true`, place valid TLS certificates (`ca.crt`, `client.crt`, `client.key`) in `/opt/empyrean/certs/`.
- [ ] **Nginx & SSL**: Ensure Let's Encrypt / Certbot SSL certificates exist for `${DOMAIN}` in `/etc/letsencrypt/live/${DOMAIN}/`.
- [ ] **Metrics Protection**: Confirm `/metrics` is protected and only accessible internally (127.0.0.1) or by your Prometheus scraper.
- [ ] **Database Migrations**: Ensure `alembic upgrade head` completed successfully on the production database.