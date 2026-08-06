# Empyrean API v1

Base URL: `/api/v1`

REST endpoints are prefixed with `/api/v1/` (the `/health` liveness check sits at root). Authentication uses **JWT HS256 Bearer tokens** (`Authorization: Bearer <access_token>`); only `POST /auth/login` and `POST /auth/refresh` are unauthenticated. All responses are JSON. Errors follow **RFC 7807 Problem JSON** (`Content-Type: application/problem+json`).

---

## Endpoint Overview

In the `Auth` column: `No` = public, `Yes` = valid JWT access token required, `Admin` = valid JWT with `role = "admin"` required.

> **Status:** `/auth/*`, `/profile*`, `/readings/*`, `/nodes/*`, `/forecast`, and `/health` are implemented (phases 1–3 + Phase 5 + Phases 7–8). The endpoint groups below (alerts, export, admin) are planned for later phases and currently return 404.

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
| `/nodes/:node_id` | PATCH | Admin | Update name, location, reading interval, or active status (pushes config to device via MQTT). Invalidates the `nodes:all` cache. |

### Alerts

> **Not implemented yet** — these endpoints return `404` until a later phase.

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/alerts` | GET | Yes | Unacknowledged threshold-breach alerts (`limit`, `offset`, `severity`) |
| `/alerts/:alert_id/acknowledge` | PATCH | Yes | Marks an alert acknowledged |

### Forecast

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/forecast` | GET | Yes | Next-60-minute AQI prediction (linear regression, retrained hourly, cached 1h) |

### Export

> **Not implemented yet** — this endpoint returns `404` until a later phase.

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/export` | GET | Yes | CSV download of raw readings for a date range |

### Profile

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/profile` | GET | Yes | Get own profile |
| `/profile` | PATCH | Yes | Update username/email/notification prefs |
| `/profile/change-password` | POST | Yes | Change password |
| `/profile` | DELETE | Yes | Delete own account |

### Admin

> **Not implemented yet** — these endpoints return `404` until a later phase.

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/admin/health` | GET | Admin | Status of MQTT broker, TimescaleDB, Redis, Celery worker/beat, DB & Redis size |
| `/admin/settings` | GET/PATCH | Admin | AQI thresholds, data retention, alert email, alerts enabled flag |

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

All request fields are validated server-side; a failed validation returns `422` (RFC 7807 problem+json).

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

Next-60-minute AQI forecast for one node, generated by a linear-regression model fit on the node's last-7-days of AQI readings (retrained hourly by the Celery beat task `tasks.forecast.retrain_model`). Served from the `celery:forecast:{node_id}` Redis cache (TTL 3600s) when present; on a cache miss the forecast is computed on the fly and cached. Redis being down degrades to computing from the DB — never a 500 on a cache problem.

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
| `GET /nodes` | 200 |
| `POST /nodes` | 200 |
| `PATCH /nodes/:node_id` | 200 |

`/profile/*` (GET/PATCH/change-password/DELETE) is **not** rate-limited. The JWT **authentication** endpoints — `register`, `login`, `refresh`, `logout` — **are** rate-limited; the caps in the table above apply to them.

> Auth caps are deliberately tight (brute-force defence) — a frontend that retries login more than 10×/min (or refresh on every tab focus) will be answered `429`.

Every response from a rate-limited endpoint carries:

- `X-RateLimit-Limit` — the window cap for that endpoint
- `X-RateLimit-Remaining` — requests left in the current window
- `X-RateLimit-Reset` — Unix epoch seconds when the window resets

On breach the API returns `429 Too Many Requests` (RFC 7807 problem+json) with the same headers attached. If Redis is unreachable the API fails open — the request proceeds and the headers are still set.
