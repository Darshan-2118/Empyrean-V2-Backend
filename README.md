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

*(Optional)* Seed sample nodes, default admin accounts, and initial configurations:
```bash
python scripts/seed.py
```

> 💡 **Default Admin Access:**
> A hardcoded admin user is available out-of-the-box for frontend integration and administrative access:
> - **Username:** `Darshan`
> - **Password:** `Darsh1812`
> - **Role:** `admin` (full permissions across all regular and `@admin_required` endpoints)

### 5. Running the Stack

#### Option A: One-Click Launch (Windows)
```bash
scripts\dev-up.bat
```
*(Auto-starts Redis in WSL if not already running, and launches Celery worker, Celery beat scheduler, and Hypercorn API server grouped into tabs inside a single Windows Terminal).*

To shut down all services and Redis:
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

**Terminal 3 — Celery Beat Scheduler (schedule files saved to `.celery/`):**
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

> 📖 For complete endpoint contracts, request/response schemas, and query parameters, see [docs/api.md](docs/api.md).

---

## 📚 Documentation

Detailed guides and specifications are available in the [`docs/`](docs/) directory:

- [Architecture & Data Flow](docs/architecture.md)
- [Getting Started Guide](docs/getting-started.md)
- [API Reference](docs/api.md)
- [Database Schema & Migrations](docs/database.md)
- [Fuzzy Inference Engine](docs/fuzzy-engine.md)
- [Production Deployment Guide](docs/deployment.md)
- [Security & Hardening](docs/security.md)
- [Testing & Quality Assurance](docs/testing.md)
- [Project Structure](docs/project-structure.md)