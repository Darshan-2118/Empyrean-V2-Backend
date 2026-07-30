# Empyrean API v1

Base URL: `/api/v1`

All endpoints return JSON. Errors use **RFC 7807 Problem JSON** (`Content-Type: application/problem+json`).

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

*Coming in Phase 5 — Redis-based, 200 requests/minute per IP.*  
Currently not enforced. Responses will include `X-RateLimit-*` headers once implemented.
