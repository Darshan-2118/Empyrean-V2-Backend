# Empyrean — Architecture

This document describes the high-level architecture of the Empyrean backend — a real-time, geospatially-aware IoT air-quality platform. It covers the system's services, end-to-end data flow, component responsibilities, and scalability considerations.

> **Status:** Phases 1–7 (scaffolding, DB models/migrations, auth & profile, MQTT ingestion, readings API, fuzzy engine, Celery tasks, forecast) are implemented. Phases 8+ (nodes/alerts/export/admin APIs, WebSocket, testing, deployment) are planned. The pipeline described below is the target architecture; the ingestion, processing, and readings/forecast API layers are live.

> **Deployment status:** the physical hardware is still in development, so the current live deployment runs a single node (`ESP32-01`). The architecture below — node registration, per-node MQTT topics, `node_id`-partitioned TimescaleDB storage — already supports many concurrent nodes and needs no backend changes to scale up as more physical nodes come online; see NFR target of ≥ 50 nodes under Performance & Reliability Targets in [security.md](security.md).

## Tech Stack

- **API Server:** Quart (async Flask) — REST endpoints, JWT auth, WebSocket push
- **Task Queue:** Celery + Redis — async fuzzy inference, anomaly detection, scheduled aggregation, alerting
- **Primary Database:** PostgreSQL 18 + TimescaleDB — time-series storage, hypertable partitioning
- **Cache / Broker:** Redis — latest-reading cache, rate limiting, Celery broker
- **MQTT Broker:** Eclipse Mosquitto (TLS/MQTTS) — device ingestion, config push, alert broadcast
- **ML Engine:** Scikit-learn + Pandas — AQI forecasting (linear regression), Z-score anomaly detection
- **Auth:** JWT (HS256) — 15-min access tokens, 7-day refresh tokens
- **Process Management:** all services run as local processes (or systemd units) on a single host — see Getting Started for running locally / in production

## Architecture Overview

The backend sits between the MQTT-publishing sensor nodes and the React frontend, and is composed of four cooperating services, all running on a single machine:

1. **MQTT Broker (Mosquitto)** — receives sensor payloads over TLS, authenticates devices via client certificates, and routes messages.
2. **MQTT Consumer** — `mqtt/client.py` (paho) subscribes to device readings + heartbeats, validates payloads, dispatches readings to Celery, and updates `Node.last_seen`.
3. **Quart API Server** — the REST/WebSocket layer the frontend talks to (auth, profile, readings, forecast).
4. **Celery Worker + Beat** — runs the Tsukamoto fuzzy inference, computes AQI, flags anomalies, generates forecasts, and checks alert thresholds on a schedule.
5. **TimescaleDB + Redis** — durable time-series storage and a fast cache layer respectively.

### End-to-End Data Flow

| Step | Description |
|------|-------------|
| 1 | Sensor node publishes a JSON reading to MQTT topic `air/node/{id}/reading` every 30s over MQTTS. |
| 2 | Mosquitto authenticates the device certificate and routes the message to the MQTT consumer (`mqtt/client.py`). |
| 3 | `mqtt/validator.py` validates the payload (Pydantic); malformed payloads are rejected and logged, never crashing the client thread. |
| 4 | The valid reading is dispatched to the Celery `tasks.process_reading` task via the Redis queue. |
| 5 | The worker runs Tsukamoto Fuzzy Inference on (Temperature, Humidity, PM2.5) to produce a 0–100 fuzzy score. |
| 6 | The worker computes the EPA AQI from PM2.5/PM10 and runs a Z-score anomaly check. |
| 7 | The enriched record is inserted into the `sensor_readings` table in PostgreSQL (will become a TimescaleDB hypertable later). |
| 8 | The `readings:latest:{node_id}` Redis key is updated (TTL 60s), invalidating the stale cache. |
| 9 | Celery Beat checks AQI thresholds every 60s; on breach, an alert row is written and pushed to connected clients over WebSocket. |
| 10 | The frontend polls `GET /api/v1/readings/latest` every 5s, hitting the Redis cache for a sub-10ms response. The response is `{ "readings": [...] }` — an array of objects with `node_id`, `time` (ISO-8601 UTC `Z`), `pm25`, `pm10`, `temperature`, `humidity`, `aqi`, `aqi_category`, `fuzzy_score`, and `is_anomaly` (no `lat`/`lon`); the frontend joins node coordinates client-side for map display. |

**Target:** end-to-end latency from sensor reading to dashboard visibility is **< 2 seconds**.

## Component Responsibilities

| Component | Technology | Responsibility |
|---|---|---|
| MQTT Broker | Eclipse Mosquitto | Message routing, TLS termination, device authentication, QoS management |
| MQTT Consumer | paho-mqtt (`mqtt/client.py`) | Subscribe to device topics, validate payloads, dispatch readings to Celery, heartbeat handling, config push |
| API Server | Quart (async Flask) | REST endpoints, JWT auth, WebSocket push, request validation |
| Task Queue | Celery + Redis | Async fuzzy inference, anomaly detection, scheduled aggregation, alerts |
| Primary DB | TimescaleDB (PostgreSQL) | Time-series storage, hypertable partitioning, continuous aggregates |
| Cache | Redis | Latest-reading cache, rate limiting, Celery broker |
| ML Engine | Scikit-learn + Pandas | ARIMA/linear regression forecasting, Z-score anomaly detection, preprocessing |

## Scalability & Maintainability Notes

- All services run as independent local processes on a single host, managed by `systemd`/`supervisord`
- The Quart API is stateless at the process level; if load ever requires more than one host, it can be scaled behind a load balancer without code changes
- Redis TTLs are tuned per data volatility (see Redis Key Schema) — live readings never stale beyond 60s, other caches longer by design
- Environment-specific config lives entirely in `.env` files — no environment branching in code

## Related Docs

- [api.md](api.md)
- [mqtt.md](mqtt.md)
- [database.md](database.md)
- [fuzzy-engine.md](fuzzy-engine.md)
- [getting-started.md](getting-started.md)
- [README](../README.md)
