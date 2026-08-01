# Empyrean-V2-Backend

Backend for the **Empyrean** application — a real-time, geospatially-aware IoT Air Quality Mapping System. It ingests sensor data over MQTT, runs it through a Tsukamoto Fuzzy Inference engine, stores it in TimescaleDB, and exposes it to the frontend via a versioned REST API.

> **Deployment status:** the physical hardware is still in development, so the current live deployment runs a single node (`ESP32-01`). The architecture — node registration, per-node MQTT topics, `node_id`-partitioned TimescaleDB storage — already supports many concurrent nodes and needs no backend changes to scale up as more physical nodes come online.

## Tech Stack

- **API Server:** Quart (async Flask) — REST endpoints, JWT auth, WebSocket push
- **Task Queue:** Celery + Redis — async fuzzy inference, anomaly detection, scheduled aggregation, alerting
- **Primary Database:** PostgreSQL + TimescaleDB — time-series storage, hypertable partitioning
- **Cache / Broker:** Redis — latest-reading cache, rate limiting, Celery broker
- **MQTT Broker:** Eclipse Mosquitto (TLS/MQTTS) — device ingestion, config push, alert broadcast
- **ML Engine:** Scikit-learn + Pandas — AQI forecasting, Z-score anomaly detection
- **Auth:** JWT (HS256) — 15-min access tokens, 7-day refresh tokens

## Documentation

All detailed docs live in [`docs/`](docs/):

| Doc | Covers |
|---|---|
| [Getting Started](docs/getting-started.md) | Prerequisites, setup, health check, verification, migrations, seeding, production |
| [Architecture](docs/architecture.md) | Services, end-to-end data flow, component responsibilities |
| [API Reference](docs/api.md) | Auth & profile endpoints (readings/nodes/alerts/etc. planned) |
| [Database Schema](docs/database.md) | Table definitions + Redis key schema |
| [MQTT Topic Schema](docs/mqtt.md) | Device topics & payloads, QoS, hardware/firmware reference |
| [Fuzzy Engine](docs/fuzzy-engine.md) | Tsukamoto inference engine — inputs, rule base, defuzzification |
| [Security & Performance](docs/security.md) | Security model, performance & reliability targets |
| [Configuration & Services](docs/configuration.md) | Environment variables, single-system deployment |
| [Frontend Integration](docs/frontend-integration.md) | Contract with the Empyrean-V2-Frontend repo |
| [Project Structure](docs/project-structure.md) | Repo directory layout |
| [Schema Plan](docs/schema-plan.md) | Database design blueprint (rationale) |
| [TODO](docs/TODO.md) | Implementation checklist |

## Quick Start

```bash
python -m venv venv && source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env                              # fill in DATABASE_URL, JWT_SECRET, ...
alembic upgrade head
python scripts/seed.py                            # admin/admin123 + defaults + sample node
python scripts/check_health.py
hypercorn app:app --bind 0.0.0.0:8000
```

The API is then available at `http://localhost:8000/api/v1/`. See [docs/getting-started.md](docs/getting-started.md) for the full guide, including Celery worker/beat, database migrations, and production setup.
