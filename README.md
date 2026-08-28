# Empyrean V2 — Air Quality Monitoring & Analytics Platform

[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![Framework](https://img.shields.io/badge/framework-Quart%20(ASGI)-brightgreen.svg)](https://pgjones.gitlab.io/quart/)
[![Database](https://img.shields.io/badge/database-PostgreSQL%20%2B%20TimescaleDB-blue.svg)](https://www.timescale.com/)
[![Broker](https://img.shields.io/badge/broker-Redis%20%2B%20MQTT-orange.svg)](https://redis.io/)
[![Async](https://img.shields.io/badge/tasks-Celery%20%2B%20Beat-green.svg)](https://docs.celeryq.dev/)

Empyrean V2 is a real-time air quality ingestion, analysis, alerting, and forecasting backend platform. Built on **Quart (async Python/ASGI)**, **Celery**, **PostgreSQL with TimescaleDB**, **Redis**, and **MQTT**, it ingests sensor telemetry from IoT nodes, processes fuzzy logic AQI ratings and anomaly detection, triggers instant alerts over WebSockets, and delivers time-series analytics and forecasting.

---

## 📑 Table of Contents

- [Architecture & Core Components](#️-architecture--core-components)
- [Prerequisites](#-prerequisites)
  - [Platform Setup (Windows / Linux)](#platform-setup-do-this-before-starting-the-stack)
- [Quick Start (Local Development)](#-quick-start-local-development)
  - [1. Clone & Set Up Python Environment](#1-clone--set-up-python-environment)
  - [2. Install Dependencies](#2-install-dependencies)
  - [3. Generate `.env` & Configure Secrets](#3-generate-env-file--configure-secrets)
  - [4. Database Setup & Migrations](#4-database-setup--migrations)
    - [`models/` & `migrations/` — what they do](#models-migrations)
    - [Admin Access — create your account](#admin-access)
  - [5. Pre-flight Stack Health Check](#5-pre-flight-stack-health-check)
  - [6. Running the Stack](#6-running-the-stack)
  - [7. Connecting a Real ESP32 Node](#7-connecting-a-real-esp32-node-optional)
- [Testing & Verification](#-testing--verification)
- [API Overview](#-api-overview)
- [Documentation](#-documentation)

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
|         | (validates schema, extracts node_id)            |
|         v                                                 |
|  [Celery Task Queue: Redis Broker]                        |
|         |                                                 |
|         +--> [process_reading] --> Anomaly Detection      |
|         |                      --> TimescaleDB Storage    |
|         |                      --> AQI Calculation        |
|         |                      --> Threshold & Alerts     |
|         |                                                 |
|         \--> [Celery Beat Schedulers]                     |
|                +-- Node offline heartbeats                |
|                +-- Daily statistical aggregations         |
|                +-- Data retention cleanup                 |
|                \-- Hourly AQI forecast updates            |
|                                                           |
|  [Quart ASGI HTTP & WebSocket Server] (Hypercorn)         |
|         +-- REST API: /api/v1/{auth, readings, nodes, ...}|
|         +-- Live WebSocket: /ws/alerts                    |
|         +-- Prometheus Metrics: /metrics                  |
|         \-- System Health: /api/v1/admin/health           |
|                                                           |
|  [Persistence & Cache Layer]                              |
|         +-- PostgreSQL + TimescaleDB (time-series)        |
|         \-- Redis (rate limiting, cache, celery broker)   |
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

### Platform Setup (do this before starting the stack)

**Windows — WSL2 + Redis.** Redis runs inside WSL2; `scripts\start.bat` starts it automatically once installed.

```powershell
# 1. Install WSL2 (elevated PowerShell; reboot if prompted, Ubuntu installs by default)
wsl --install

# 2. Inside WSL, install and start Redis
wsl
sudo apt update && sudo apt install -y redis-server
sudo service redis-server start
```

**Linux — Redis only.**

```bash
sudo apt update && sudo apt install -y redis-server    # Debian/Ubuntu
sudo systemctl enable --now redis-server
```

Verify: `redis-cli ping` (`wsl redis-cli ping` on Windows) must return `PONG`.

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

### 3. Generate `.env` File & Configure Secrets

Initialize your `.env` file with 256-bit cryptographically secure production secrets:
```bash
python scripts/generate_secrets.py --write-env
```

*(Note: `generate_secrets.py` automatically initializes `.env` from `.env.example` if it does not already exist. If `.env` is already present, it protects your configuration and displays `.env is already present` without overwriting).*

Alternatively, you can copy `.env.example` manually:
```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

> 🔒 **Security Notice:** The application enforces strict fail-fast validation in `config/__init__.py`. It will reject development placeholders (such as `dev-secret-key`, `dev-jwt-secret`, or `change-me-*`), keys shorter than 32 bytes, or low-entropy secrets. Running `scripts/generate_secrets.py` ensures your secrets comply with production constraints.

Update your `.env` file with your `DATABASE_URL`, `REDIS_URL`, and `MQTT_BROKER_HOST`.

### 4. Database Setup & Migrations

Ensure PostgreSQL with TimescaleDB is running, then apply database migrations:
```bash
alembic upgrade head
```

<a id="models-migrations"></a>

> 📁 **`models/` vs `migrations/` in one line each:** `models/` holds the SQLAlchemy ORM classes — the source of truth for every table. `migrations/` holds versioned Alembic schema changes, and `alembic upgrade head` applies any not yet run so the database stays in sync with the models.

*(Optional)* Seed the sample node and initial system settings:
```bash
python scripts/seed.py
```

> 🧪 **Simulated node:** the seeder creates a pseudo node `ESP32-01` so you can verify the full ingestion → AQI → alerting pipeline without any hardware. With the stack running, publish a synthetic reading and check `GET /api/v1/readings/latest`:
> ```bash
> mosquitto_pub -h localhost -t "air/node/ESP32-01/reading" -m '{"temperature": 27.5, "humidity": 60.0, "pressure": 1013.0, "voc_ohm": 120000.0, "mq135_ppm": 15.0, "pm25": 18.0, "pm10": 35.0}'
> ```

> 📖 **Need help installing or configuring PostgreSQL & TimescaleDB?**  
> See our step-by-step [PostgreSQL & TimescaleDB Setup Guide](docs/database-setup.md) for Docker, Windows (WSL2 / Native), Linux, macOS, and Cloud setup, or watch this [TimescaleDB Installation Video Tutorial (YouTube)](https://youtu.be/KlOGfFzLdqA).

<a id="admin-access"></a>

> 💡 **Admin Access:**
> There are no hardcoded credentials — you create your own admin account:
> ```bash
> python scripts/create_admin.py
> ```
> It prompts for a username, email, and password (hidden input; must be ≥ 8 chars with upper, lower, digit, and symbol). If the username already exists it is promoted to admin — add `--reset-password` to set a fresh password on an existing or locked-out account. For non-interactive/CI deploys, set `BOOTSTRAP_ADMIN_USERNAME`, `BOOTSTRAP_ADMIN_PASSWORD`, and (optionally) `BOOTSTRAP_ADMIN_EMAIL` in `.env` instead.

### 5. Pre-flight Stack Health Check

Before starting the server, run the health check script to validate Python imports, database tables, TimescaleDB hypertable, Redis connectivity, and configuration:
```bash
python scripts/check_health.py
```

> ℹ️ **Note on Redis Connectivity:**
> If you have not started your Redis server yet, the Redis check may report `[FAIL]`. This is expected during initial setup because `scripts\start.bat` automatically initializes the Redis service in WSL upon launch. If you prefer to verify a completely green health check beforehand, start Redis first (`wsl sudo -n /usr/sbin/service redis-server start`) or re-run `python scripts/check_health.py` after starting the stack.

### 6. Running the Stack

#### Option A: One-Click Launch (Windows)
```bash
scripts\start.bat
```
*(Auto-starts Redis in WSL as a systemd service if not already running, waits until it answers PING, and launches WSL Instance (VM keep-alive), Celery worker, Celery beat scheduler, and Hypercorn API server grouped into tabs inside a single Windows Terminal).*

To shut down all services and Redis:
```bash
scripts\stop.bat
```

#### Option B: Manual Service Launch

**Terminal 1 — Redis Server (if using WSL on Windows):**
```bash
wsl sudo -n /usr/sbin/service redis-server start
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

### 7. Connecting a Real ESP32 Node (Optional)

`ESP32-01` is only a stand-in for testing. To wire in a physical ESP32 (BME680 + MQ135 + PMS5003/SDS011):

1. In `.env`, set `MQTT_ENABLED=true` and point `MQTT_BROKER_HOST` / `MQTT_BROKER_PORT` at your broker (ingestion stays off until `MQTT_ENABLED=true`).
2. Register the device with `POST /api/v1/nodes` (or reuse the seeded `ESP32-01`).
3. Flash firmware that speaks the topic contract — the `node_id` in the topic is authoritative and must match the registered node:

| Direction | Topic | JSON payload |
|-----------|-------|--------------|
| Device → Backend | `air/node/{node_id}/reading` | `temperature, humidity, pressure, voc_ohm, mq135_ppm, pm1, pm25, pm10, battery_v` |
| Device → Backend | `air/node/{node_id}/status` | `online, battery_v, firmware` (heartbeat) |
| Backend → Device | `air/node/{node_id}/config` | `interval_s, fuzzy_enabled, enabled` |

Payload field ranges and a broker smoke test are documented in [docs/testing.md](docs/testing.md).

---

## 🧪 Testing & Verification

Empyrean ships with two levels of testing, each serving a distinct purpose. You should run them before pushing code, after changing configuration, and after setting up a new environment to catch regressions early and confirm the entire stack is wired correctly.

### Layer 1 — Full Stack Verification (recommended before committing)

Runs infrastructure checks (Postgres reachability, Alembic migration currency, TimescaleDB hypertable, Redis PING, seed data) followed by the app factory smoke check. **Requires the stack to be running.**

```bash
# Quick infra + health checks only (default)
python scripts/verify.py

# Full suite — also runs the entire pytest test suite
python scripts/verify.py --full
```

> 💡 `check.bat` is a convenience wrapper around `verify.py` for Windows users: `scripts\check --full`

### Layer 2 — pytest Unit & Integration Tests

The full test suite covers phase-level behaviour, API contract enforcement, fuzzy inference edge cases, MQTT dispatch rules, and more.

```bash
# Run the full suite
pytest

# Run with verbose output
pytest tests/ -v

# Run a specific test file
pytest tests/test_phase_coverage.py -v
```

> 📖 For test organisation, coverage goals, and how to write new tests, see [docs/testing.md](docs/testing.md).

### Verify Live Service Health

Once the stack is running, confirm all components are healthy via the admin endpoint (requires an admin JWT):

```bash
curl -H "Authorization: Bearer <admin_access_token>" http://localhost:8000/api/v1/admin/health
```

For an unauthenticated liveness check, use `GET /health` at the root.

---

## 📡 API Overview

| Group | Endpoint | Method | Description |
|-------|----------|--------|-------------|
| **Auth** | `/api/v1/auth/register` | `POST` | Register a new user |
| | `/api/v1/auth/login` | `POST` | Authenticate and obtain JWT token pair |
| | `/api/v1/auth/refresh` | `POST` | Rotate and issue fresh access/refresh tokens |
| | `/api/v1/auth/logout` | `POST` | Revoke refresh token and the presented access token |
| **Profile** | `/api/v1/profile` | `GET` / `PATCH` / `DELETE` | Current user profile (view, update, deactivate) |
| | `/api/v1/profile/change-password` | `POST` | Change password (revokes all sessions) |
| **Readings** | `/api/v1/readings/latest` | `GET` | Latest telemetry across nodes |
| | `/api/v1/readings/history` | `GET` | Aggregated time-series history (`time_bucket`) |
| **Export** | `/api/v1/export` | `GET` | Streaming CSV export of raw readings |
| **Nodes** | `/api/v1/nodes` | `GET` / `POST` | List and register IoT sensor nodes |
| | `/api/v1/nodes/<node_id>` | `PATCH` | Node configuration updates |
| **Alerts** | `/api/v1/alerts` | `GET` | List unacknowledged alerts |
| | `/api/v1/alerts/<id>/acknowledge` | `PATCH` | Acknowledge an alert |
| **Forecast** | `/api/v1/forecast?node_id=<node_id>` | `GET` | Short-term AQI trend forecast |
| **System** | `/api/v1/admin/health` | `GET` | Component health diagnostics (admin) |
| | `/api/v1/admin/settings` | `GET` / `PATCH` | System settings registry (admin) |
| | `/metrics` | `GET` | Prometheus instrumentation metrics |
| **WebSockets** | `/ws/alerts` | `WS` | Real-time threshold breach notifications |

> 📖 For complete endpoint contracts, request/response schemas, and query parameters, see [docs/api.md](docs/api.md).

---

## 📚 Documentation

Detailed guides and specifications are available in the [`docs/`](docs/) directory:

- [Architecture & Data Flow](docs/architecture.md)
- [Getting Started Guide](docs/getting-started.md)
- [Database & TimescaleDB Setup Guide](docs/database-setup.md)
- [Database Schema & Migrations](docs/database.md)
- [API Reference](docs/api.md)
- [Fuzzy Inference Engine](docs/fuzzy-engine.md)
- [Production Deployment (Getting Started § Production)](docs/getting-started.md#production-deployment)
- [Security & Hardening](docs/security.md)
- [Testing & Quality Assurance](docs/testing.md)
- [Project Structure](docs/project-structure.md)