# Empyrean — Frontend Integration

The frontend (`Empyrean-V2-Frontend` — React + Leaflet + Recharts) lives in a separate repo and talks to this backend purely over HTTP/WebSocket. There's no shared codebase — just a URL and an auth contract both sides agree on.

## 1. CORS

The frontend dev server and this API run on different origins/ports, so the backend must explicitly allow the frontend's origin. Origins are configured via the `CORS_ORIGINS` env var (comma-separated).

Add every environment the frontend is served from (local dev port, staging, production domain).

## 2. Base URL the frontend should point at

The frontend should never hardcode the API host — it reads it from an env var:

```
# frontend/.env
VITE_API_BASE_URL=http://localhost:8000/api/v1
VITE_WS_URL=ws://localhost:8000/ws/alerts
```

In production these become `https://<your-domain>/api/v1` and `wss://<your-domain>/ws/alerts`. Every fetch/axios call in the frontend should build off `VITE_API_BASE_URL`, matching the routes documented in [api.md](api.md) (e.g. `${VITE_API_BASE_URL}/readings/latest`).

## 3. Auth handshake

1. Frontend POSTs `username`/`password` to `/auth/login`, receives `access_token`, `refresh_token`, `expires_in`, and `role`.
2. Frontend stores tokens in memory (not localStorage, per the security requirements) and attaches `Authorization: Bearer <access_token>` to every subsequent request.
3. `role` (`user` / `admin`) drives which routes/nav items the frontend renders — admin-only pages should call `/admin/*` and `/nodes` PATCH endpoints only when `role === "admin"`.
4. When a request comes back `401` with an expired-token detail, the frontend should silently call `/auth/refresh` with the stored `refresh_token`, get a new `access_token`, and retry the original request once. If refresh also fails (`401`), force logout and redirect to `/`.

## 4. Live data contract

- **Polling:** the dashboard map polls `GET /readings/latest` every 5 seconds and `GET /nodes` less frequently (it's cached 300s server-side) — no backend change needed to adjust this, it's purely a frontend interval.
- **Push (planned — Phase 9):** the frontend opens a WebSocket to `VITE_WS_URL` to receive `air/alerts` broadcasts in real time (threshold-breach toasts) instead of polling `/alerts`. The backend bridges the MQTT `air/alerts` topic onto this WebSocket. The WebSocket endpoint is not implemented yet.
- **Rate limits (enforced since Phase 5):** readings endpoints are limited to 200 requests/minute per IP. The frontend should respect `X-RateLimit-Remaining` and back off polling if it starts hitting `429`; `X-RateLimit-Reset` (Unix epoch seconds) tells when the window clears. The dashboard polls `/readings/latest` every 5s (~12 req/min, comfortably under the limit), but keep the backoff logic anyway. `Retry-After` is not sent — use `X-RateLimit-Reset`.

## 5. Local dev — running both together

Since they're separate repos, run each in its own terminal (or use `concurrently` from a root script):

```bash
# terminal 1 — backend (see [docs/getting-started.md](getting-started.md))
cd Empyrean-V2-Backend
source venv/bin/activate
hypercorn app:app --bind 0.0.0.0:8000
# plus celery worker/beat in their own terminals

# terminal 2 — frontend
cd Empyrean-V2-Frontend && npm run dev
```

Confirm connectivity with `curl http://localhost:8000/api/v1/admin/health` (once authenticated) or by checking the browser network tab for successful `/readings/latest` calls with no CORS errors.

## 6. Keeping the contract in sync

Since backend and frontend evolve independently, treat [docs/api.md](api.md) (or an OpenAPI spec generated from it) as the source of truth for field names and shapes. Any breaking change to a response schema should be released under `/api/v2/` per the versioning policy, so the existing frontend deployment keeps working against `/api/v1/` until it's updated.

## Related Docs

- [api.md](api.md)
- [getting-started.md](getting-started.md)
- [README](../README.md)
