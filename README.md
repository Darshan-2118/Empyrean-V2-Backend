# Empyrean V2 — Air Quality Monitoring & Analytics Platform

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
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
  - [0. The "First Time Ever" Checklist](#0-the-first-time-ever-checklist-do-this-in-order)
  - [1. Clone & Set Up Python Environment](#1-clone--set-up-python-environment)
  - [2. Install Dependencies](#2-install-dependencies)
  - [3. Generate `.env` & Configure Secrets](#3-generate-env-file--configure-secrets)
  - [4. Database Setup & Migrations](#4-database-setup--migrations)
    - [`models/` & `migrations/` — what they do](#models-migrations)
    - [Admin Access — create your account](#admin-access)
  - [5. Pre-flight Stack Health Check](#5-pre-flight-stack-health-check)
  - [6. Running the Stack](#6-running-the-stack)
  - [7. Connecting a Real ESP32 Node](#7-connecting-a-real-esp32-node-optional)
  - [8. Every Script at a Glance](#8-every-script-at-a-glance)
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
| **Python** | `3.12` | Required runtime environment (the health check enforces this) |
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

<a id="first-time-checklist"></a>

### 0. The "First Time Ever" Checklist (do this in order)

Brand new to the project? Run these **exactly in this order**, one at a time, and
**stop reading after each step until you finish it**. If a step shows an error,
scroll down to the matching step in the numbered sections below for what it means
and how to fix it.

> ⏱️ **Total time:** roughly 10–15 minutes the very first time.

| # | What to run | What it does | Good sign |
|---|-------------|--------------|-----------|
| 0️⃣ | `git clone https://github.com/Darshan-2118/Empyrean-V2-Backend.git` then `cd Empyrean-V2-Backend` | Downloads the project and steps into its folder | You see the repo folder |
| 1️⃣ | `python -m venv .venv` | Creates a private "sandbox" for Python packages so they don't touch the rest of your computer | A `.venv` folder appears |
| 2️⃣ | `.\.venv\Scripts\activate` (Windows) / `source .venv/bin/activate` (Linux/macOS) | "Turns on" the sandbox | You see `(.venv)` at the start of your prompt |
| 3️⃣ | `pip install -r requirements.txt` | Installs every library the project needs | Lots of "Successfully installed ..." lines, no red errors |
| 4️⃣ | `python scripts/generate_secrets.py --write-env` | Creates your `.env` file (your private settings + secret keys). Do this **once**. | `Successfully updated .env with new production secrets.` |
| 5️⃣ | Edit `.env` — set `DATABASE_URL`, `REDIS_URL`, `MQTT_BROKER_HOST` to match **your** setup | Tells the app where your database, Redis, and MQTT broker live | Your values are filled in |
| 6️⃣ | `alembic upgrade head` | Creates/updates all database tables automatically | Prints `Running upgrade -> 0001 ... 0009` |
| 7️⃣ | `python scripts/seed.py` | Fills the database with starter data (sample node `ESP32-01`, default settings) | `Seed completed` (or similar) with no errors |
| 8️⃣ | `python scripts/create_admin.py` | Creates **your** personal admin login for the app | `Admin user '...' created` |
| 9️⃣ | `python scripts/check_health.py` | The "doctor check-up" — verifies everything is connected | `[OK]` on all sections (Redis may be `[FAIL]` if not started yet) |
| 🔟 | `scripts\start.bat` (Windows) | Starts the whole app (server, Celery workers, and Redis in WSL) | "Dev stack launched successfully." |

> ⚠️ **If anything above gives you an error, don't panic** — go to the step with the
> same number in the detailed instructions below. Every step explains the common
> errors and exactly how to fix them. You can also re-run `python scripts/check_health.py`
> at any time to see what's still wrong.

---

### 1. Clone & Set Up Python Environment

First, copy the project to your computer and create an isolated Python environment
(a "venv"). Think of the venv as your project's own personal toolbox — it keeps all
the packages for this project separate from the rest of your computer, so nothing
breaks.

```bash
# 1. Clone the repository (downloads the project to a folder on your computer)
git clone https://github.com/Darshan-2118/Empyrean-V2-Backend.git
cd Empyrean-V2-Backend

# 2. Create the virtual environment (the "toolbox")
# Windows PowerShell / CMD
python -m venv .venv
.\.venv\Scripts\activate

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

> ✅ **How do I know this worked?** Your command prompt should now start with
> `(.venv)`, like `(.venv) C:\Users\you\Empyrean-V2-Backend>`.
>
> ❌ **I get "python is not recognized"?** Python isn't installed or isn't on your
> PATH. Install Python 3.12+ from [python.org](https://www.python.org/) and check the
> **"Add Python to PATH"** box during installation, then close and reopen your terminal.

### 2. Install Dependencies

Now install all the libraries the project needs. This reads the list from
`requirements.txt` and downloads everything automatically.

```bash
pip install -r requirements.txt
```

> ✅ **Good sign:** the command finishes with "Successfully installed ..." and returns
> to your prompt with no red text.
>
> ⏳ **First time is slow.** This downloads many packages and can take a few minutes.

### 3. Generate `.env` File & Configure Secrets

The app reads its settings from a file named `.env` in the project root. This file
holds your private keys — treat it like a diary, **never share it or commit it to git**.

Run the secret generator exactly once. It creates `.env` from the template
`.env.example` and fills in strong random secret keys automatically:

```bash
python scripts/generate_secrets.py --write-env
```

> ✅ **Good sign:**
> ```text
> Successfully updated .env with new production secrets.
> ```
>
> 📝 **Already ran it before?** If `.env` already exists the script will tell you
> `".env is already present"` and **won't overwrite** your settings — that's correct
> and safe. To force a fresh one you'd add `--force`, but you almost never need to.

Alternatively, you can copy `.env.example` manually:
```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

> 🔒 **Security Notice:** The application enforces strict fail-fast validation in `config/__init__.py`. It will reject development placeholders (such as `dev-secret-key`, `dev-jwt-secret`, or `change-me-*`), keys shorter than 32 bytes, or low-entropy secrets. Running `scripts/generate_secrets.py` ensures your secrets comply with production constraints.

**Now open `.env` in any text editor and update these three lines to match YOUR setup:**

| Setting | What it is | Example |
|---------|------------|---------|
| `DATABASE_URL` | Where your PostgreSQL database lives (host, port, db name, user, password) | `postgresql://myuser:mypass@localhost:5432/Empyrean` |
| `REDIS_URL` | Where your Redis server lives | `redis://localhost:6379/0` |
| `MQTT_BROKER_HOST` | Where your MQTT broker lives | `localhost` |

> ❌ **I don't have a database yet?** See step 4 first — you'll need PostgreSQL +
> TimescaleDB running. The [PostgreSQL & TimescaleDB Setup Guide](docs/database-setup.md)
> walks you through installing it on Windows, Linux, or macOS.

### 4. Database Setup & Migrations

Make sure PostgreSQL (with the TimescaleDB extension) is **running**, then let the
app build its tables. You do this with a single command:

```bash
alembic upgrade head
```

> ✅ **Good sign:** lines like
> ```text
> Running upgrade  -> 0001_initial_schema, 0002_add_timescaledb_hypertable, ... 0009
> ```
> and then a plain prompt with no error.
>
> ❌ **`psycopg2.OperationalError` / "connection refused"?** The database isn't
> running, or `DATABASE_URL` in `.env` is wrong. Start PostgreSQL, double-check
> `DATABASE_URL`, and try again.
>
> ❌ **"database 'Empyrean' does not exist"?** You need to create the database first
> (see the setup guide above), e.g. `CREATE DATABASE "Empyrean";` in `psql`.

<a id="models-migrations"></a>

> 📁 **`models/` vs `migrations/` in one line each:** `models/` holds the SQLAlchemy ORM classes — the source of truth for every table. `migrations/` holds versioned Alembic schema changes, and `alembic upgrade head` applies any not yet run so the database stays in sync with the models.

**Seed the database** — this fills it with a starter kit (default system settings and
a pretend sensor node called `ESP32-01`) so you can test the whole pipeline without
any real hardware:

```bash
python scripts/seed.py
```

> ✅ **Good sign:** log lines like `Seeded ...` / `Created admin user` with no errors.
>
> ℹ️ **About the admin user:** by default `seed.py` creates the sample data but NOT a
> login account. If you don't set `SEED_ADMIN_PASSWORD`, it tells you to use
> `create_admin.py` next — that's exactly what the next block below is for.

> 🧪 **Simulated node:** the seeder creates a pseudo node `ESP32-01` so you can verify the full ingestion → AQI → alerting pipeline without any hardware. With the stack running, publish a synthetic reading and check `GET /api/v1/readings/latest`:
> ```bash
> mosquitto_pub -h localhost -t "air/node/ESP32-01/reading" -m '{"temperature": 27.5, "humidity": 60.0, "pressure": 1013.0, "voc_ohm": 120000.0, "mq135_ppm": 15.0, "pm25": 18.0, "pm10": 35.0}'
> ```

> 📖 **Need help installing or configuring PostgreSQL & TimescaleDB?**  
> See our step-by-step [PostgreSQL & TimescaleDB Setup Guide](docs/database-setup.md) for Docker, Windows (WSL2 / Native), Linux, macOS, and Cloud setup, or watch this [TimescaleDB Installation Video Tutorial (YouTube)](https://youtu.be/KlOGfFzLdqA).

<a id="admin-access"></a>

**Create your admin account** — there are no hardcoded logins in this project. You
make your own, and it becomes the account you use to sign into the app:

```bash
python scripts/create_admin.py
```

> ✅ **Good sign:** after answering the prompts, you see something like
> `Admin user '<your_username>' created`.
>
> 🔑 **The password rules** (it will keep asking until you get these right):
> - at least **8 characters**, at most **72**
> - must contain an **uppercase** letter (A–Z)
> - a **lowercase** letter (a–z)
> - a **digit** (0–9)
> - and a **symbol** (like `!@#$`)
>
> ℹ️ **Already have a user with that name?** It gets promoted to admin. Use
> `python scripts/create_admin.py --reset-password` to set a fresh password on an
> existing or locked-out account. For non-interactive/CI deploys, set
> `BOOTSTRAP_ADMIN_USERNAME`, `BOOTSTRAP_ADMIN_PASSWORD`, and (optionally)
> `BOOTSTRAP_ADMIN_EMAIL` in `.env` instead.

### 5. Pre-flight Stack Health Check

Before starting the server, run the health check script. It's like a doctor's
check-up for your whole stack — it verifies Python, your database, all tables, the
TimescaleDB hypertable, Redis, and your configuration, and prints a clear `[OK]` or
`[FAIL]` for each:

```bash
python scripts/check_health.py
```

> ✅ **Good sign:** every section prints `[OK]`, ending with `ALL CHECKS PASSED`.
>
> ⚠️ **Redis shows `[FAIL]`?** That's expected if Redis isn't running yet —
> `scripts\start.bat` (next step) starts it automatically on Windows. See the
> Redis note below.
>
> ❌ **A database-related `[FAIL]`?** Re-check step 4: is PostgreSQL running? Did
> `alembic upgrade head` finish? Is `DATABASE_URL` correct?

> ℹ️ **Note on Redis Connectivity:**
> If you have not started your Redis server yet, the Redis check may report `[FAIL]`. This is expected during initial setup because `scripts\start.bat` automatically initializes the Redis service in WSL upon launch. If you prefer to verify a completely green health check beforehand, start Redis first (`wsl sudo -n /usr/sbin/service redis-server start`) or re-run `python scripts/check_health.py` after starting the stack.

### 6. Running the Stack

#### Option A: One-Click Launch (Windows)
```bash
scripts\start.bat
```
*(Auto-starts Redis in WSL as a systemd service if not already running, waits until it answers PING, and launches WSL Instance (VM keep-alive), Celery worker, Celery beat scheduler, and Hypercorn API server grouped into tabs inside a single Windows Terminal).*

> ✅ **Good sign:** the last line is `Dev stack launched successfully.`
> The API will be at **http://localhost:8000** — open it in a browser to see the
> liveness endpoint (`GET /health`).

To shut down all services and Redis:
```bash
scripts\stop.bat
```

#### Option B: Manual Service Launch

Prefer to start each piece yourself in separate terminals? Do it in this order —
**Redis first**, then the workers, then the server:

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

### 8. Every Script at a Glance

A quick reference to every script in `scripts/` and what it does, so you always know
which one to reach for:

| Script | When to run it | What it does |
|--------|----------------|--------------|
| `generate_secrets.py --write-env` | **First time only** | Creates `.env` with strong random secret keys |
| `alembic upgrade head` | After cloning, or after pulling new code | Builds/updates every database table to the latest version |
| `seed.py` | After migrations, first time | Fills the DB with starter data (sample node, default settings) |
| `create_admin.py` | After seeding, first time | Creates your personal admin login for the web app |
| `check_health.py` | Whenever something seems broken | Doctor's check-up: verifies DB, tables, TimescaleDB, Redis, config |
| `verify.py` | Before committing / pushing | Runs the infra checks; add `--full` to also run the whole pytest suite |
| `start.bat` | To start the app (Windows) | Launches server + Celery workers + Redis (in WSL) in one go |
| `stop.bat` | To stop the app (Windows) | Stops everything `start.bat` launched |
| `db.sh` | Database tasks (Linux/macOS) | Quick `psql` access, migrations, seeding — reads credentials from `.env` for you |
| `bench.py` | Load testing the API | Tiny HTTP load generator against a URL (default `http://127.0.0.1:8000/health`) |
| `banner.py` | Used by `start.bat` | Just prints the pretty startup banners (you don't run this yourself) |

> 💡 **Every script works no matter which folder you run it from.** They all figure
> out the project's location themselves, read `.env` from the project root, and use
> your currently-active Python environment — so a fresh clone on a different machine
> "just works".

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

> 💡 **Windows quick-check:** `scripts\check.bat --full` is a shortcut that runs
> `verify.py --full` for you in Command Prompt (it's the same thing).

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