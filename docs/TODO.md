# Empyrean V2 Backend — Project Status

**All 14 phases complete** — production-ready IoT Air Quality Mapping System backend.

## Milestones

| Milestone | Description | Date |
|-----------|-------------|------|
| **M1** | Foundation — scaffolding, config, DB, models, migrations | 2026-08-05 |
| **M2** | Auth — registration, login, JWT, profiles | 2026-08-06 |
| **M3** | Ingestion — MQTT consumer, payload validation | 2026-08-06 |
| **M4** | Core Processing — Fuzzy engine, Celery tasks, AQI | 2026-08-07 |
| **M5** | API Layer — Readings, Forecast, Nodes, Alerts, Export | 2026-08-07 |
| **M6** | Real-time — WebSocket alert broadcasting | 2026-08-07 |
| **M7** | Admin & Ops — Health, Settings registry | 2026-08-07 |
| **M8.1** | Hardening — Known-issues backlog resolved | 2026-08-06 |
| **M8.2** | Exhaustive Testing — 241-test suite | 2026-08-11 |
| **M8.3** | Deployment — systemd, nginx, metrics, perf baseline | 2026-08-12 |

## Key Metrics

- **Tests:** 253 passing (`pytest -q` ~6:25)
- **Perf:** 1,038 RPS, 59ms p95, 0 errors (targets: ≥100 RPS, <200ms p95)
- **Endpoints:** 22 REST + WebSocket + `/health` + `/metrics`
- **Deploy artifacts:** `deploy/` — systemd, nginx, logrotate, deploy.sh

## Quick Commands

```bash
# Full test suite
venv/Scripts/python.exe -m pytest -q

# Performance baseline
venv/Scripts/python.exe -m hypercorn 'app:create_app()' --bind 127.0.0.1:8000 &
venv/Scripts/python.exe scripts/bench.py

# Production deploy
./deploy/deploy.sh  # (requires SERVER_HOST, SERVER_USER env vars)
```

## Documentation

| File | Purpose |
|------|---------|
| `api.md` | Complete REST/WebSocket reference |
| `deployment.md` | Production deployment guide |
| `architecture.md` | System design |
| `database.md` | Schema & TimescaleDB setup |
| `configuration.md` | Environment variables |
| `getting-started.md` | Local dev setup |
| `fuzzy-engine.md` | Tsukamoto inference details |
| `security.md` | Auth, rate-limiting, hardening |
| `project-structure.md` | Codebase map |

---

*All phases complete. Ready for production.*