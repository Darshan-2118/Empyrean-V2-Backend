# Empyrean V2 — Manual Testing & Hardware Simulation Guide

This guide details how to verify and test the entire Empyrean V2 backend **without requiring physical IoT hardware (ESP32/sensors) to be connected**.

---

## 📑 Table of Contents
1. [Overview & Testing Strategy](#1-overview--testing-strategy)
2. [Prerequisites & Environment Setup](#2-prerequisites--environment-setup)
3. [Automated Verification & Unit Tests](#3-automated-verification--unit-tests)
4. [Starting the Backend Stack](#4-starting-the-backend-stack)
5. [Simulating Physical IoT Sensor Nodes](#5-simulating-physical-iot-sensor-nodes)
6. [Testing REST Endpoints](#6-testing-rest-endpoints)
7. [Testing Real-Time WebSockets](#7-testing-real-time-websockets)
8. [Verifying Database & Celery Processing](#8-verifying-database--celery-processing)
9. [Troubleshooting & Common Checks](#9-troubleshooting--common-checks)

---

## 1. Overview & Testing Strategy

Empyrean ingests sensor data via **MQTT topics**, queues processing via **Celery & Redis**, computes EPA AQI & Tsukamoto fuzzy scores, stores records in **PostgreSQL / TimescaleDB**, and delivers live metrics over **WebSockets** and **REST APIs**.

```
[ Mock / Script ] ──(MQTT)──▶ [ MQTT Broker ] ──▶ [ Ingestion Client ]
                                                         │
                                                  (Celery / Redis)
                                                         ▼
[ REST / WebSockets ] ◀── [ TimescaleDB / Cache ] ◀── [ Processing Worker ]
```

Because the ingestion pipeline expects standard JSON payloads over MQTT topics, you can completely mock hardware nodes with scripts or MQTT clients.

---

## 2. Prerequisites & Environment Setup

Ensure the virtual environment and configuration are ready:

```powershell
# 1. Activate your virtual environment
.\.venv\Scripts\activate

# 2. Ensure dependencies are installed
pip install -r requirements.txt

# 3. Ensure .env has valid keys and credentials
python scripts/generate_secrets.py --write-env

# 4. Run database migrations & seed initial admin / sample nodes
alembic upgrade head
python scripts/seed.py
```

---

## 3. Automated Verification & Unit Tests

Before launching services, you can run the built-in test suites to verify that models, schemas, fuzzy logic, and API routes work in isolation.

### A. Full Stack Health Probe
Validates DB connection, required tables, TimescaleDB hypertable status, Redis ping, and app factory:
```powershell
python scripts/check_health.py
```

### B. Pure-Logic Smoke Phases
Verifies module imports, app factory, route registration, JWT round-trips, fuzzy engine bounds, MQTT dispatch, and CSV export logic. **No services required.**

Phases 2–12 run concurrently — the full 12-phase check typically completes in under 30 seconds. Each phase reports its individual wall-clock time so regressions are immediately visible.
```powershell
python scripts/smoke_phases.py
```

### C. Full Stack Verification (recommended before committing)
Runs infrastructure checks (Postgres, Alembic migration, Redis) plus the health probe. By default pytest is **not** included so this stays fast:
```powershell
# Quick infra + health checks only (default, no pytest)
python scripts/verify.py

# Full suite — includes the entire pytest test suite
python scripts/verify.py --full
```
> Windows shortcut: `scripts\check` or `scripts\check --full`

### D. Full Unit & Integration Test Suite
Covers phase-level behaviour, API contract enforcement, fuzzy inference edge cases, MQTT dispatch rules, and more:
```powershell
# Run all tests
pytest

# Verbose output with short tracebacks
pytest tests/ -v --tb=short

# Run a specific test file
pytest tests/test_phase_coverage.py -v
```

---

## 4. Starting the Backend Stack

### Option A: Windows Quick Launch
Auto-starts Redis in WSL (if not already running) and launches Celery Worker, Celery Beat Scheduler, and Hypercorn API server grouped into tabs in a single Windows Terminal:
```powershell
# Launch stack:
.\scripts\dev-up.bat

# Stop stack and Redis:
.\scripts\dev-down.bat
```

### Option B: Manual Process Launch (Separate Terminals)
- **Terminal 1 (Redis):** `wsl redis-server --daemonize yes`
- **Terminal 2 (Celery Worker):** `celery -A celery_app.celery_app worker --loglevel=info`
- **Terminal 3 (Celery Beat):** `celery -A celery_app.celery_app beat --loglevel=info`
- **Terminal 4 (API Server):** `hypercorn "app:create_app()" --bind 0.0.0.0:8000`

---

## 5. Simulating Physical IoT Sensor Nodes

### MQTT Ingestion Contract
- **Telemetry Topic:** `air/node/<node_id>/reading`
- **Status Topic:** `air/node/<node_id>/status`
- **Default Seed Node ID:** `ESP32-01`

### Method 1: Python Simulation Script

Create a script `simulate_node.py` or run it interactively:

```python
import json
import time
import paho.mqtt.client as mqtt

BROKER = "localhost"
PORT = 1883
NODE_ID = "ESP32-01"
TOPIC = f"air/node/{NODE_ID}/reading"

client = mqtt.Client()
client.connect(BROKER, PORT, 60)

# Simulated sensor readings (BME680 + PMS5003 + MQ135)
payload = {
    "node_id": NODE_ID,
    "temperature": 26.8,     # °C (-40 to 60)
    "humidity": 58.4,        # % (0 to 100)
    "pressure": 1012.5,      # hPa (300 to 1250)
    "voc_ohm": 135000.0,     # Gas resistance
    "mq135_ppm": 14.2,       # Air quality sensor
    "pm1": 8.0,              # PM1.0 (µg/m³)
    "pm25": 16.5,            # PM2.5 (µg/m³)
    "pm10": 29.0,            # PM10 (µg/m³)
    "battery_v": 4.12        # Battery voltage
}

print(f"Publishing mock reading to {TOPIC}...")
client.publish(TOPIC, json.dumps(payload))
print("Published successfully!")
client.disconnect()
```

### Method 2: Command Line (`mosquitto_pub`)
```bash
mosquitto_pub -h localhost -t "air/node/ESP32-01/reading" -m '{"temperature": 27.5, "humidity": 60.0, "pm25": 18.0, "pm10": 35.0, "pressure": 1013.0, "voc_ohm": 120000.0, "mq135_ppm": 15.0}'
```

### Method 3: MQTT GUI Tools (MQTTX / MQTT Explorer)
1. Connect to `localhost:1883`.
2. Publish JSON to topic `air/node/ESP32-01/reading`.

---

## 6. Testing REST Endpoints

Use Postman, Thunder Client, or `curl`.

### 1. Check API Liveness & System Health
```http
GET http://localhost:8000/health
GET http://localhost:8000/admin/health
```

### 2. User Authentication
```http
POST http://localhost:8000/api/v1/auth/login
Content-Type: application/json

{
  "username": "admin",
  "password": "your_admin_password"
}
```
*Response includes an `access_token`. Pass this in the `Authorization: Bearer <token>` header for protected routes.*

### 3. Retrieve Latest Live Reading
```http
GET http://localhost:8000/api/v1/readings/live?node_id=ESP32-01
Authorization: Bearer <token>
```

### 4. Query Historical Time-Series Readings
```http
GET http://localhost:8000/api/v1/readings/history?node_id=ESP32-01&limit=50
Authorization: Bearer <token>
```

### 5. Fetch Forecast Data
```http
GET http://localhost:8000/api/v1/forecast?node_id=ESP32-01
Authorization: Bearer <token>
```

---

## 7. Testing Real-Time WebSockets

You can connect using Postman (WebSocket Request), `wscat`, or browser developer tools.

### A. Live Telemetry Stream
- **URL:** `ws://localhost:8000/ws/live`
- Publish a mock message via MQTT while connected to this WebSocket. You should see the ingested data broadcast immediately.

### B. Live Alerts Stream
- **URL:** `ws://localhost:8000/ws/alerts`
- Publish an extreme PM2.5 value (e.g. `"pm25": 250.0`) to trigger an alert and verify the instant breach notification.

---

## 8. Verifying Database & Celery Processing

### Check Celery Worker Console
When a reading is published, the Celery worker window will log:
```
[INFO] Task tasks.readings.process_reading[...] received
[INFO] Processed reading for node ESP32-01: AQI=..., FuzzyScore=...
```

### Check Database Rows in PostgreSQL
Connect via `psql` or pgAdmin / DBeaver:
```sql
-- Check ingested sensor records:
SELECT time, node_id, temperature, humidity, pm25, aqi, fuzzy_score 
FROM sensor_readings 
ORDER BY time DESC 
LIMIT 10;

-- Check recorded alerts:
SELECT * FROM alerts ORDER BY created_at DESC LIMIT 10;
```

---

## 9. Troubleshooting & Common Checks

| Issue | Cause | Solution |
|---|---|---|
| **Redis connection error** | Redis service not running | Run `wsl redis-server --daemonize yes` |
| **MQTT messages not consumed** | Ingestion client not started / wrong broker | Verify `MQTT_BROKER_HOST` in `.env` and check API logs |
| **Missing DB tables** | Migrations not applied | Run `alembic upgrade head` |
| **Node ID rejected** | Node ID contains invalid characters | Use alphanumeric IDs with hyphens/underscores (e.g. `ESP32-01`) |
