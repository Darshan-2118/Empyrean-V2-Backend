# Empyrean API v1

Base URL: `/api/v1`

REST endpoints are prefixed with `/api/v1/` (the `/health` liveness check sits at root). Authentication uses **JWT HS256 Bearer tokens** (`Authorization: Bearer <access_token>`); only `POST /auth/login` and `POST /auth/refresh` are unauthenticated. All responses are JSON **except `GET /export`, which streams a CSV attachment, and `GET /metrics`, which returns Prometheus text exposition format (`text/plain`)**. Errors follow **RFC 7807 Problem JSON** (`Content-Type: application/problem+json`).

---

## Endpoint Overview

In the `Auth` column: `No` = public, `Yes` = valid JWT access token required, `Admin` = valid JWT with `role = "admin"` required.

> **Status:** `/auth/*`, `/profile*`, `/readings/*`, `/nodes/*`, `/alerts/*`, `/forecast`, `/admin/*`, `/export`, `/health`, and `/metrics` are implemented (phases 1–3 + Phase 5 + Phases 7–11 + Phase 14).

### Authentication

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/auth/register` | POST | No | Register a new user and auto-login (returns `access_token`, `refresh_token`, `expires_in`, `role`) |
| `/auth/login` | POST | No | Returns `access_token`, `refresh_token`, `expires_in`, `role` |
| `/auth/refresh` | POST | No | Exchanges a refresh token for a new access token |
| `/auth/logout` | POST | No | Revokes a refresh token |

### Sensor Readings

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/readings/latest` | GET | Yes | Latest reading per node. Redis-cached, TTL 60s. Polled every 5s by the dashboard. |
| `/readings/history` | GET | Yes | Time-bucketed historical readings (`from`, `to`, `node_id`, `bucket`) via `time_bucket()` over `sensor_readings` |

### Nodes

> **Live since Phase 8.** `PATCH` pushes the reading interval to the device via MQTT (fail-open — a broker outage does not fail the update). `POST` and `PATCH` both invalidate the Redis `nodes:all` cache so the served list is never stale past a mutation; `PATCH` also drops `readings:latest` when `is_active` changes.

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/nodes` | GET | Yes | All registered nodes with metadata. Redis-cached, TTL 300s. |
| `/nodes` | POST | Yes | Self-service registration of a new sensor node (any authenticated user). Invalidates the `nodes:all` cache. |
| `/nodes/:node_id` | PATCH | Admin | Update name, location, reading interval, or active status (pushes config to device via MQTT, fail-open). Invalidates the `nodes:all` cache. Response includes `config_pushed` (bool): whether the MQTT config push to the device succeeded (`false` if the broker/client was unavailable, but the update is still persisted). |

### Alerts

> **Live since Phase 9.** `GET /alerts` returns **unacknowledged** threshold-breach alerts, newest first, with `limit` (`1..200`), `offset`, and `severity` (`warning`|`critical`) filters. The response body is `{"alerts": [...], "total": <int>}`, where `total` is the count of all unacknowledged alerts before pagination. The full unacknowledged list is cached under `alerts:unacked` (TTL 30s) and filters/pagination are applied in-memory after the cache read, so the cache key never varies with query params. `PATCH /alerts/:alert_id/acknowledge` marks an alert acknowledged (**idempotent** — acknowledging an already-acknowledged alert is a no-op) and invalidates the cache. Alert *creation* runs in the Celery beat task (`tasks.alerts.check_thresholds`), not here.

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/alerts` | GET | Yes | Unacknowledged threshold-breach alerts (`limit`, `offset`, `severity`) |
| `/alerts/:alert_id/acknowledge` | PATCH | Yes | Marks an alert acknowledged |

### WebSocket — `/ws/alerts`

A **broadcast-only** push socket. The server pushes `air/alerts` MQTT messages to every connected client through the connection manager; it never echoes or responds to client frames (push only, no client→server messaging).

- **Auth:** JWT via the `Authorization: Bearer <access_token>` header (non-browser clients) or the `?token=<access_token>` query param (browser WebSockets cannot set headers).
- **Handshake:** the JWT is validated **before** `accept()` — an unauthenticated or invalid-token handshake is closed rather than accepted.
- **Payload:** the `air/alerts` MQTT message `{ node_id, aqi, category, severity, timestamp }`, broadcast as JSON.
- The socket is only live once the MQTT broker publishes to `air/alerts`; with no broker a client connects but receives nothing until a broadcast arrives.

### Forecast

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/forecast` | GET | Yes | Next-60-minute AQI prediction (linear regression, retrained hourly, cached 1h) |

### Export

> **Live since Phase 11.** `GET /export` streams the **raw** `sensor_readings` rows in a date range as an RFC 4180 CSV attachment (`Content-Disposition: attachment`). It is **not** admin-only — any authenticated user can download raw data (matching `/readings/*`). The response is **streamed in ~64 KB chunks** from a server-side cursor, so a one-year export never loads into memory; all validation errors (`422`) are returned **before** streaming starts, never mid-CSV.

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/export` | GET | Yes | Streaming CSV download of raw readings (`from`, `to`, `node_id`) |

#### GET `/export`

**Query params** (all optional, mirroring `/readings/history`):

| Param | Type | Default | Description |
|---|---|---|---|
| `from` | ISO-8601 datetime | 24h ago | Start of range (inclusive). Naive timestamps treated as UTC. |
| `to` | ISO-8601 datetime | now | End of range (inclusive). Naive timestamps treated as UTC. |
| `node_id` | string | all nodes | Restrict to a single node. Unknown node → empty CSV. |

The span `to − from` is capped at **365 days** (`MAX_EXPORT_SPAN`, matching the default data-retention window); a wider request gets `422` rather than a silent truncation.

**CSV schema** — 15 columns, header row = the model column names in order:

```
time,node_id,temperature,humidity,pressure,voc_ohm,mq135_ppm,pm1,pm25,pm10,battery_v,fuzzy_score,aqi,aqi_category,is_anomaly
```

Cells: `time` is ISO-8601 UTC with a trailing `Z` (e.g. `2026-08-10T12:00:00Z`); `None` → empty string; `is_anomaly` → lowercase `true`/`false`; everything else via `str()` (floats like `42.0`, `aqi` int like `101`). Rows are ordered by `time`, then `node_id` (chronological, deterministic). Comma-containing values are auto-quoted (RFC 4180).

**Response headers:** `Content-Type: text/csv; charset=utf-8`, `Content-Disposition: attachment; filename="readings_export_<from>_<to>.csv"` (bounds formatted `%Y%m%dT%H%M%SZ`, no colons/spaces), `Cache-Control: no-store`, plus `X-RateLimit-*` from the 200/min per-IP cap.

**Errors:** `401` (missing/invalid token), `422` (malformed `from`/`to`, `from` ≥ `to`, or span over 365 days), `429` (rate limited) — all RFC 7807 problem+json.

### Profile

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/profile` | GET | Yes | Get own profile |
| `/profile` | PATCH | Yes | Update username/email/notification prefs |
| `/profile/change-password` | POST | Yes | Change password |
| `/profile` | DELETE | Yes | Delete own account |

### Admin

> **Live since Phase 10.** All admin routes require `role = "admin"` (`@admin_required`) — a non-admin token gets `403`, no token gets `401`. Like `/profile/*`, they are **not** rate-limited (privileged, low-volume calls). Both endpoints return RFC 7807 problem+json on error.

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/admin/health` | GET | Admin | Status of MQTT broker, TimescaleDB, Redis, Celery worker/beat, DB & Redis size |
| `/admin/settings` | GET/PATCH | Admin | AQI thresholds, data retention, alert email, alerts enabled flag |

#### GET `/admin/health`

Per-component system health. **Fail-soft:** an unreachable component reports `degraded` inside the body — the endpoint itself **always returns `200`** (matching the liveness `/health`), so the caller, not the status code, interprets component state. Overall `status` is `ok` only when every check is `ok`, else `degraded`.

**Success response** `200 OK`:

```json
{
  "status": "ok",
  "checks": {
    "database":      { "status": "ok",       "detail": "PostgreSQL reachable" },
    "timescaledb":   { "status": "ok",       "detail": "sensor_readings is a hypertable" },
    "redis":         { "status": "ok",       "detail": "PING ok, 42 keys" },
    "mqtt":          { "status": "degraded", "detail": "ingestion client not running (MQTT_ENABLED unset or startup failed)" },
    "celery_worker": { "status": "degraded", "detail": "no worker responded to ping" },
    "celery_beat":   { "status": "degraded", "detail": "no heartbeat yet — beat has not ticked since startup" }
  },
  "sizes": {
    "database_bytes": 12345678,
    "redis_keys": 42,
    "redis_used_memory_bytes": 1048576
  }
}
```

- `database` — `SELECT 1` + `pg_database_size(current_database())` (`error` only if the DB is unreachable).
- `timescaledb` — confirms `sensor_readings` is a real hypertable; extension absent ⇒ `degraded` (system works, just un-optimized).
- `redis` — `PING` + `DBSIZE` + `INFO memory` (used_memory).
- `mqtt` — the ingestion client's broker connection (`is_connected()`).
- `celery_worker` — `celery_app.control.ping(timeout=2)` in a thread; ≥1 reply ⇒ `ok`.
- `celery_beat` — freshness of the `celery:heartbeat:beat` stamp that `tasks.alerts.check_thresholds` (the 60s beat task) writes; ≤180s old (3× the schedule) ⇒ `ok`. This is the liveness proof that beat is actually firing scheduled work, since beat publishes no heartbeat of its own.

**Errors:** `401` (no token), `403` (non-admin).

#### GET `/admin/settings`

All effective system settings. `system_settings` rows win; known knobs with no row yet fall back to config (or a fixed default) with `source: "config"` so the admin sees the effective value. Non-registry rows already in the table are also returned.

**Success response** `200 OK`:

```json
{
  "settings": [
    { "key": "aqi_warning_threshold", "value": "100", "description": "AQI value that triggers a warning alert", "updated_at": "2026-08-07T10:00:00Z", "updated_by": 1, "source": "db" },
    { "key": "aqi_critical_threshold", "value": "150", "description": "AQI value that triggers a critical alert", "updated_at": null, "updated_by": null, "source": "config" },
    { "key": "data_retention_days", "value": "365", "description": "How long raw readings are retained before purging", "updated_at": null, "updated_by": null, "source": "config" },
    { "key": "alerts_enabled", "value": "true", "description": "Master toggle for alert generation", "updated_at": null, "updated_by": null, "source": "config" },
    { "key": "alert_email", "value": "", "description": "Email address that receives critical alerts", "updated_at": null, "updated_by": null, "source": "config" }
  ]
}
```

#### PATCH `/admin/settings`

Update the known knobs; only provided fields change. Values are stored as text in `system_settings` (bools → `"true"`/`"false"`, `null` email → `""`). Changes take effect on the next beat/cleanup tick — the Celery tasks read `system_settings` fresh each run (e.g. `PATCH {data_retention_days: 30}` is honored by the `data_retention_cleanup` task).

**Request body** (all fields optional):

```json
{
  "aqi_warning_threshold": 90,
  "aqi_critical_threshold": 160,
  "data_retention_days": 30,
  "alerts_enabled": false,
  "alert_email": "ops@example.com"
}
```

| Key | Type | Range / Rule |
|---|---|---|
| `aqi_warning_threshold` | int | 0–500; must stay `< aqi_critical_threshold` |
| `aqi_critical_threshold` | int | 0–500 |
| `data_retention_days` | int | 1–3650 |
| `alerts_enabled` | bool | — |
| `alert_email` | `string` | valid email, or `null` to clear |

**Success response** `200 OK` — the fresh full settings list (same shape as GET), with each changed row now `source: "db"` and `updated_by` stamped with the admin's user id.

**Errors:** `401` (no token), `403` (non-admin), `422` (validation: out-of-range value, invalid email, unknown key — `extra="forbid"` rejects typos — or `aqi_warning_threshold >= aqi_critical_threshold`), `400` (missing/non-object body).

---

## Authentication

Auth endpoints are **unauthenticated** (no token required). All other endpoints require a valid JWT access token.

### POST `/auth/register`

Create a new user account and receive JWT tokens immediately (auto-login).

**Request body:**

```json
{
  "username": "johndoe",
  "email": "john@example.com",
  "password": "securepass123"
}
```

**Success response** `201 Created`:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "a1b2c3d4e5f6...",
  "expires_in": 900,
  "role": "user",
  "user": {
    "id": 1,
    "username": "johndoe",
    "email": "john@example.com",
    "role": "user"
  }
}
```

**Errors:** `400` (missing body), `409` (username/email taken), `422` (validation).

---

### POST `/auth/login`

Authenticate with username and password.

**Request body:**

```json
{
  "username": "johndoe",
  "password": "securepass123"
}
```

**Success response** `201 Created`:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "a1b2c3d4e5f6...",
  "expires_in": 900,
  "role": "user",
  "user": {
    "id": 1,
    "username": "johndoe",
    "email": "john@example.com",
    "role": "user"
  }
}
```

**Errors:** `401` (invalid credentials/deactivated), `422` (validation).

---

### POST `/auth/refresh`

Exchange a refresh token for a new access+refresh pair (token rotation — old token is revoked).

**Request body:**

```json
{
  "refresh_token": "a1b2c3d4e5f6..."
}
```

**Success response** `200 OK`:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "new_token_here...",
  "expires_in": 900,
  "role": "user",
  "user": {
    "id": 1,
    "username": "johndoe",
    "email": "john@example.com",
    "role": "user"
  }
}
```

**Errors:** `401` (invalid/expired/revoked token).

---

### POST `/auth/logout`

Revoke a refresh token. Returns `204` regardless of whether the token was valid (no information leakage).

**Request body:**

```json
{
  "refresh_token": "a1b2c3d4e5f6..."
}
```

**Success:** `204 No Content` (no body).

**Errors:** `400` (missing body), `422` (validation).

---

### Auth Token Lifecycle

| Token | Type | Lifetime | Storage |
|-------|------|----------|---------|
| Access | JWT (HS256) | 15 minutes | Client memory (never localStorage) |
| Refresh | Opaque (random) | 7 days | Client memory + hashed in DB |

**Header format for protected routes:**

```
Authorization: Bearer <access_token>
```

**Refresh flow:** When the API returns `401`, the frontend should:
1. Call `POST /auth/refresh` with the stored refresh token
2. On success, retry the original request with the new access token
3. On failure (`401`), force logout

---

## Request validation

All request-body fields are validated server-side through the Phase 12 `@validate_body` middleware (`api/validation.py`): every JSON-body endpoint is wrapped with its Pydantic schema, and the validated model is passed to the handler. A missing or malformed body returns `400`; any other schema violation returns `422` (RFC 7807 problem+json). The per-field rules below are what the schemas enforce.

| Field | Endpoints | Rule |
|---|---|---|
| `username` | register, PATCH `/profile` | 3–50 chars, letters / digits / `_` only. Surrounding whitespace is stripped **before** the length check, so `"  a  "` fails the 3-char minimum. |
| `email` | register, PATCH `/profile` | valid email address, ≤255 chars, stored lowercase. |
| `password` | register, `change-password` | 6–72 **bytes** UTF-8 (bcrypt limit — multi-byte characters count by byte, not by character). |
| `current_password` / `new_password` | `change-password` | both required; `new_password` must satisfy the password rule above. |
| `refresh_token` | `refresh`, `logout` | required opaque token string. |
| `notification_prefs` | PATCH `/profile` | free-form JSON object, stored and echoed back verbatim. |

JSON request bodies must set `Content-Type: application/json`.

> **Note:** `login` and `register` return **`201 Created`**, not `200`. Treat any `2xx` as success rather than asserting `200`.

---

## Profile

All profile endpoints require a valid JWT access token (`Authorization: Bearer <token>`).

### GET `/profile`

Get the current user's profile.

**Success response** `200 OK`:

```json
{
  "id": 1,
  "username": "johndoe",
  "email": "john@example.com",
  "role": "user",
  "notification_prefs": {},
  "is_active": true,
  "last_login_at": "2026-07-30T14:30:00Z",
  "created_at": "2026-07-27T10:00:00Z",
  "updated_at": "2026-07-30T14:30:00Z"
}
```

---

### PATCH `/profile`

Update profile fields. Only send the fields you want to change.

**Request body:**

```json
{
  "username": "johndoe_new",
  "email": "john_new@example.com",
  "notification_prefs": {
    "email_on_critical": true
  }
}
```

All fields are optional. Omitting a field leaves it unchanged.

**Success response** `200 OK` (updated profile object, same shape as GET).

**Errors:** `400` (missing body), `409` (username/email already taken), `422` (validation).

---

### POST `/profile/change-password`

Change the current user's password.

**Request body:**

```json
{
  "current_password": "oldpass123",
  "new_password": "newpass456"
}
```

**Success response** `200 OK`:

```json
{
  "message": "Password changed successfully"
}
```

**Errors:** `400` (missing body), `401` (current password incorrect), `422` (validation).

---

### DELETE `/profile`

Soft-delete the current user's account (sets `is_active = false` and revokes all refresh tokens).

**Success response** `200 OK`:

```json
{
  "message": "Account deleted successfully"
}
```

No request body needed.

---

## Readings

All readings endpoints require a valid JWT access token (`Authorization: Bearer <token>`) and are rate-limited per IP (200 requests/minute — see [Rate Limiting](#rate-limiting)).

### GET `/readings/latest`

Latest **enriched** reading for every **active** node. A single `DISTINCT ON (node_id)` query (not one per node); the result is cached under the global Redis key `readings:latest` for 60s (the only key this endpoint serves/writes). Per-node `readings:latest:{node_id}` keys are maintained by the ingestion task (`tasks/process_reading`), which also invalidates the global key on every write-through so a cached `/readings/latest` never stays stale past the just-persisted reading (L-28).

**Query params:** none.

**Success response** `200 OK`:

```json
{
  "readings": [
    {
      "node_id": "ESP32-01",
      "time": "2026-08-03T14:30:00Z",
      "temperature": 28.4,
      "humidity": 62.1,
      "pressure": 1012.5,
      "pm25": 12.3,
      "pm10": 20.1,
      "battery_v": 3.9,
      "fuzzy_score": 42.7,
      "aqi": 51,
      "aqi_category": "Moderate",
      "is_anomaly": false
    }
  ]
}
```

Metric fields are `null` when a node has not reported them yet. Returns `{"readings": []}` when no active nodes have readings.

**Errors:** `401` (missing/invalid token), `429` (rate limited).

---

### GET `/readings/history`

Time-bucketed averages over the `sensor_readings` hypertable using TimescaleDB `time_bucket()`.

**Query params** (all optional):

| Param | Type | Default | Description |
|---|---|---|---|
| `from` | ISO-8601 datetime | 24h ago | Start of range (inclusive). Naive timestamps treated as UTC. |
| `to` | ISO-8601 datetime | now | End of range (inclusive). Naive timestamps treated as UTC. |
| `node_id` | string | all nodes | Restrict to a single node. |
| `bucket` | string | `1h` | Bucket interval — one of `1m`, `5m`, `15m`, `1h`, `6h`, `1d`. |

**Success response** `200 OK`:

```json
{
  "buckets": [
    {
      "bucket": "2026-08-03T14:00:00Z",
      "node_id": "ESP32-01",
      "avg_temperature": 28.2,
      "avg_humidity": 61.8,
      "avg_pm25": 11.9,
      "avg_pm10": 19.7,
      "avg_aqi": 49.5,
      "max_aqi": 55,
      "min_aqi": 44,
      "reading_count": 120
    }
  ]
}
```

`avg_*` fields are averages over the readings in the bucket; `max_aqi`/`min_aqi` are the extreme AQI values; `reading_count` is the number of readings aggregated. Returns `{"buckets": []}` when no readings fall in the range.

**Errors:** `401` (missing/invalid token), `422` (malformed `from`/`to`, `from` ≥ `to`, or unknown `bucket`), `429` (rate limited).

---

## Forecast

All forecast endpoints require a valid JWT access token (`Authorization: Bearer <token>`) and are rate-limited per IP (200 requests/minute — see [Rate Limiting](#rate-limiting)).

### GET `/forecast`

Next-60-minute AQI forecast for one node, generated by a linear-regression model fit on the node's last-7-days of AQI readings (retrained hourly by the Celery beat task `empyrean.tasks.forecast.retrain_model`). Served from the `celery:forecast:{node_id}` Redis cache (TTL 3600s) when present; on a cache miss the forecast is computed on the fly and cached. Redis being down degrades to computing from the DB — never a 500 on a cache problem.

**Query params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `node_id` | string | yes | Node to forecast for. |

**Success response** `200 OK`:

```json
{
  "node_id": "ESP32-01",
  "horizon_minutes": 60,
  "points": [
    {
      "time": "2026-08-03T14:31:00Z",
      "aqi": 48.2
    }
  ]
}
```

`points` is a 1-point-per-minute list of 60 entries. Returns `points: []` when the node has too little data (< 30 non-null AQI readings in 7 days) to train a model.

**Errors:** `401` (missing/invalid token), `422` (missing `node_id`), `404` (unknown `node_id`), `429` (rate limited).

---

## Monitoring

### GET `/metrics`

Prometheus metrics exposition endpoint. Exposes `empyrean_http_requests_total` (counter by method, route, status) and `empyrean_http_request_duration_seconds` (histogram by method, route, status). **Internal only** — nginx restricts access to `127.0.0.1` so Prometheus must scrape from the host or an internal IP.

**No auth required** (protected by network-level restriction).

**Success response** `200 OK` — Prometheus text format.

---

## Health

### GET `/health`

Simple liveness check (no auth required).

**Success response** `200 OK`:

```json
{
  "status": "ok",
  "environment": "development"
}
```

---

## Error Format

All errors follow RFC 7807:

```json
{
  "type": "about:blank",
  "title": "Unauthorized",
  "status": 401,
  "detail": "Invalid username or password"
}
```

| Status | Meaning |
|--------|---------|
| `400` | Bad request (missing body, malformed JSON) |
| `401` | Unauthorized (missing/invalid token, bad credentials) |
| `403` | Forbidden (admin-only route) |
| `404` | Resource not found |
| `409` | Conflict (duplicate username/email) |
| `413` | Request Entity Too Large (request body exceeds 64 KB) — `application/problem+json` |
| `422` | Validation error (invalid field values) |
| `429` | Rate limited |
| `500` | Internal server error |

---

## CORS

The API accepts requests from origins configured in `CORS_ORIGINS` (comma-separated env var).  
Credentials are supported (for `Authorization` headers).

**Current allowed origins:** `http://localhost:3000`, `http://localhost:5173`

---

## Rate Limiting

Redis-backed fixed-window rate limiting is applied **per endpoint**, so each endpoint has its own per-IP budget and one endpoint cannot exhaust another's. The window key is the **trusted remote address** (`request.remote_addr` only — a client-supplied `X-Forwarded-For` first entry is never trusted, H-5 hardening) plus the current UTC minute plus the endpoint scope: `ratelimit:{endpoint}:{ip}:{minute}` (e.g. `ratelimit:readings.latest:192.0.2.10:202608051530`).

| Endpoint(s) | Cap (requests/minute/IP) |
|---|---|
| `POST /auth/register` | **5** |
| `POST /auth/login` | **10** |
| `POST /auth/refresh` | **10** |
| `POST /auth/logout` | **10** |
| `GET /readings/latest` | 200 |
| `GET /readings/history` | 200 |
| `GET /forecast` | 200 |
| `GET /export` | 200 |
| `GET /nodes` | 200 |
| `POST /nodes` | 200 |
| `PATCH /nodes/:node_id` | 200 |

`/profile/*` (GET/PATCH/change-password/DELETE) is **not** rate-limited. The JWT **authentication** endpoints — `register`, `login`, `refresh`, `logout` — **are** rate-limited; the caps in the table above apply to them.

> Auth caps are deliberately tight (brute-force defence) — a frontend that retries login more than 10×/min (or refresh on every tab focus) will be answered `429`.

---

## Request Logging

Phase 12 installs app-wide `before_request` / `after_request` hooks (`api/request_log.py`) that emit exactly **one INFO record per HTTP request** on the dedicated `empyrean.request` logger, carrying four fields — `method`, `path`, `status`, `duration_ms`. The logger is separate from the app logger so it can be tuned or filtered independently.

Security rules: request bodies, `Authorization` headers, and query strings are never logged — `path` is the path only (no `?token=...`), and client IPs are omitted entirely (never read from a client-supplied `X-Forwarded-For`, same rule as rate limiting). WebSocket handshakes are not HTTP requests and are not logged. For the streaming `/export` endpoint the duration covers validation + response setup, not the stream send.

Every response from a rate-limited endpoint carries:

- `X-RateLimit-Limit` — the window cap for that endpoint
- `X-RateLimit-Remaining` — requests left in the current window
- `X-RateLimit-Reset` — Unix epoch seconds when the window resets

On breach the API returns `429 Too Many Requests` (RFC 7807 problem+json) with the same headers attached. If Redis is unreachable the API fails open — the request proceeds and the headers are still set.
