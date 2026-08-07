# Empyrean — Database Schema

This document is the concise runtime reference for the database tables and Redis keys. For the design rationale, table-by-table justifications, and the migration strategy, see [schema-plan.md](schema-plan.md).

Seven tables implemented via SQLAlchemy 2.0 + Alembic migrations:

## `users` — who logs in
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

## `refresh_tokens` — session management
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

## `nodes` — sensor devices
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

## `sensor_readings` — the core data
**Note:** Already a TimescaleDB hypertable (converted by migration `b2bab23ab3c0`; partitioned on `time`, default 7-day chunks). Compression/retention policies are still pending.

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

## `hourly_agg` — pre-computed summaries
Currently a regular table (will become a TimescaleDB continuous aggregate later).

| Column | Type | Notes |
|--------|------|-------|
| `bucket` | `TIMESTAMPTZ` (PK) | Hour start |
| `node_id` | `VARCHAR(50)` (PK, FK → nodes) | |
| `avg_temperature` / `avg_humidity` / `avg_pm25` / `avg_pm10` | `REAL` | Hourly averages |
| `max_aqi` / `min_aqi` / `avg_aqi` | `SMALLINT` / `REAL` | AQI stats |
| `anomaly_count` | `INTEGER` | Anomalies that hour |
| `reading_count` | `INTEGER` | Total readings |

## `alerts` — threshold-breach notifications
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

## `system_settings` — configurable knobs
| Column | Type | Notes |
|--------|------|-------|
| `key` | `VARCHAR(100) PK` | e.g. `'aqi_warning_threshold'` |
| `value` | `TEXT` | Stored as text, cast when needed |
| `description` | `VARCHAR(255)` | Human explanation |
| `updated_at` | `TIMESTAMPTZ` | |
| `updated_by` | `INTEGER FK → users` | ON DELETE SET NULL |

Default settings seeded: `aqi_warning_threshold=100`, `aqi_critical_threshold=150`, `data_retention_days=365`, `alerts_enabled=true`

## Redis Key Schema

> **Status:** Live since Phase 5: `readings:latest`, `readings:latest:{node_id}`, and `ratelimit:{endpoint}:{ip}:{minute}`. Phase 7 added `celery:forecast:{node_id}` and `forecast:model:{node_id}`. Phase 8 added `nodes:all`. Phase 9 added `alerts:unacked` (30s TTL). Phase 10 added `celery:heartbeat:beat`. The remaining keys land with their phases.

| Key Pattern | TTL | Value |
|---|---|---|
| `readings:latest` | 60s | Latest enriched reading per active node (JSON array) |
| `readings:latest:{node_id}` | 60s | Latest enriched reading (JSON) — write-through from `tasks.process_reading`; the same write-through also `DEL`etes the global `readings:latest` key (L-28) so the served cache is never stale past a just-persisted reading |
| `nodes:all` | 300s | All node metadata (JSON array); PATCH `/nodes/:node_id` invalidates `readings:latest` when `is_active` changes |
| `alerts:unacked` | 30s | Unacknowledged alerts (JSON array) — added in Phase 9; written by `GET /alerts`, invalidated by `PATCH /alerts/:alert_id/acknowledge` |
| `celery:heartbeat:beat` | 3600s | ISO-8601 UTC timestamp of the last beat tick — written by `tasks.alerts.check_thresholds` (the 60s beat task, so this doubles as beat liveness); read by `GET /admin/health`, which reports `celery_beat` healthy while the stamp is ≤180s old (3× the schedule) |
| `ratelimit:{endpoint}:{ip}:{minute}` | 60s | Request count (int) — per endpoint, per IP, per UTC minute (e.g. `ratelimit:auth.login:192.0.2.10:202608051530`); each endpoint has its own per-IP budget, so one endpoint cannot exhaust another's |
| `celery:forecast:{node_id}` | 3600s | AQI forecast array (JSON) — read/written by the `/forecast` endpoint & `generate_forecast` |
| `forecast:model:{node_id}` | 3600s | Trained linear model `{"slope", "intercept", "trained_at"}` (JSON) — written by `tasks.forecast.retrain_model` |

TTLs are tuned per data volatility, not a single blanket value: live readings never go stale beyond 60s, while less time-sensitive data (node metadata, forecasts) uses a longer TTL to cut down on recomputation.

## Related Docs

- [schema-plan.md](schema-plan.md)
- [architecture.md](architecture.md)
- [README](../README.md)
