# Empyrean — Security & Performance Targets

This document describes the security model of the Empyrean V2 backend and the non-functional performance and reliability targets the system is designed to meet.

## Security

- JWT HS256, 15-min access / 7-day refresh token expiry
- MQTT over TLS (MQTTS) in production; plaintext (port 1883) supported for local dev
- REST API over HTTPS only; HTTP redirects to HTTPS (planned for production deployment)
- Passwords hashed with bcrypt (cost factor ≥ 12)
- API rate-limited to 200 requests/minute per IP via Redis (enforced since Phase 5): readings endpoints enforce a fixed window keyed on client IP, return `429` on breach, and always attach `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`
- All inputs validated with Pydantic schemas
- Devices authenticate to the MQTT broker with unique client certificates
- DB credentials passed via environment variables — never hardcoded

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
