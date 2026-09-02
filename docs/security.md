# Empyrean — Security & Performance Targets

This document describes the security model of the Empyrean V2 backend and the non-functional performance and reliability targets the system is designed to meet.

## Security

- JWT HS256 (algorithm pinned at startup), 15-min access / 7-day refresh token expiry
- Token revocation: logout, password change, and account deactivation revoke all refresh tokens **and** blocklist the presented access token's `jti` in Redis (self-cleaning TTL); refresh-token reuse is detected and the whole family revoked
- **Password reset:** `POST /auth/forgot-password` is opaque against account enumeration — it always returns `202` with the same message whether or not the email exists. Reset tokens are single-use, expiry-gated (60 min), and stored **only as SHA-256 digests** (never raw). One active reset link per account (a new request invalidates any prior unredeemed token). `POST /auth/reset-password` returns a generic `401` for invalid/expired/used tokens (no token-guessing feedback) and, on success, revokes all refresh tokens and invalidates the user cache so every session must re-authenticate. Both endpoints are rate-limited (`forgot-password` 5/min, `reset-password` 10/min).
- MQTT over TLS (MQTTS) in production; plaintext (port 1883) supported for local dev
- REST API over HTTPS only; HTTP redirects to HTTPS (planned for production deployment)
- Passwords hashed with bcrypt (cost factor ≥ 12); max password size 72 UTF-8 bytes; weak/known-breached secrets rejected at registration and password change
- Rate limiting is Redis-backed and **per endpoint** per IP: credential/session endpoints are tightly capped (`register` 5/min; `login`, `refresh`, `logout`, `forgot-password` 5/min; `reset-password`, `change-password` 10/min — brute-force defence), data endpoints default to 200/min; breaches return `429` with `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset` headers
- WebSocket `/ws/alerts` requires a valid, non-revoked JWT at handshake and re-authenticates every 15 minutes (`4401` close otherwise)
- All inputs validated with Pydantic schemas; HTTP bodies capped at 64 KB (`413` above)
- Devices authenticate to the MQTT broker with unique client certificates
- DB credentials passed via environment variables — never hardcoded; no hardcoded admin account (bootstrap admin is env-driven and strength-gated)
- `/metrics` is network-restricted to localhost and optionally gated by a `METRICS_SECRET` header

## Performance & Reliability Targets

| Metric | Target |
|---|---|
| Sensor → dashboard end-to-end latency | < 2s |
| MQTT broker acknowledgement time | < 300ms |
| REST API 95th-percentile response | < 200ms |
| Concurrent MQTT messages handled | ≥ 50 nodes |
| API throughput | ≥ 100 RPS |
| 30-day time-range aggregate query | < 100ms (TimescaleDB) |
| System uptime | ≥ 99% |

## Related Docs

- [configuration.md](configuration.md)
- [api.md](api.md)
- [README](../README.md)
