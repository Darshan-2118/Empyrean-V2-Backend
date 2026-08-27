# Fixed Bugs Log

> Every fix from the audit in [`docs/bugs.md`](bugs.md), grouped by severity.
> IDs are stable for traceability; full detail lives in git history. Landed
> 2026-08-25 → 2026-08-27 across six waves (HIGH ×2, MEDIUM ×3, cosmetic pass).

---

## 🔴 HIGH (all 20 findings)

| # | Fix |
|---|-----|
| H1+H11 | CORS allowlist hardened: strict `scheme://host[:port]` origins; wildcard/empty production allowlist = startup failure (`config`). |
| H2 | problem+json details sanitised — whitespace collapse + 200-char cap (`app_factory/helpers`). |
| H3 | `/health` no longer leaks APP_ENV; diagnostics moved behind admin auth (`/admin/health`). |
| H4/M66 | JWT decode pinned to literal `"HS256"`; config rejects any other algorithm at startup. |
| H5/H6/H28 | Hardcoded admin creds removed; provisioned from `BOOTSTRAP_ADMIN_*` env (bcrypt). ⚠ Rotate the old `Darshan` DB row. |
| H7/H16 | Access-token revocation via Redis jti blocklist; password change revokes too. Fails open without Redis. |
| H8+M9 | Refresh-token reuse → whole chain revoked + internal Redis reuse counter (generic 401, no oracle). |
| H9 | WS handshake uses the canonical JWT decode path (shared helper). |
| H10 | OPTIONS preflights short-circuit 204 before rate-limit decorators. |
| H12/H31 | Opt-in `TRUST_PROXY_HEADERS` honors nginx `X-Real-IP` so proxied clients get real per-IP buckets. |
| H13+H26 | `notification_prefs` got a real schema — HttpUrl-only webhooks, bounded knobs, 50-cap, JSON-safe persist. |
| H15 | Soft-delete now says *deactivated*; retention cleanup anonymises stale users in place (PII scrubbed, FK history kept). |
| H17 | Explicit `"notification_prefs": null` clears prefs (pydantic `model_fields_set`); absent key stays a no-op. |
| H18 | Per-user export cooldown (Redis `SET NX EX`, 5 min default) → 429 with retry hint. |
| H19 | `/metrics` gated by optional `METRICS_SECRET` header (403 on mismatch). |
| H20 | MQTT publisher refuses plaintext when the broker port is TLS-only (8883/8884). |
| H21 | One canonical node-id regex — schemas import `_NODE_ID_RE` from `mqtt.config`. |
| H22 | Publisher `_encode()` guard — unencodable payloads log-and-drop instead of killing the beat task. |
| H23 | Cached forecast models validated (`trained_at` parse + 48 h freshness) before use. |
| H24 | Alert severity rank from a fixed map, int-cast before SQL interpolation. |
| H25 | Device timestamps clamped to ±24 h of server time. |
| H27 | VARCHAR(255) byte-vs-char audit verified safe; documented in `models/user.py`. |
| H29 | `generate_secrets --write-env` chmods `.env` to 0600. |
| H30 | nginx: TLSv1.2+ floor + HSTS. |
| H32 | hypercorn `--workers 1` — multi-worker breaks async Quart shared state. |
| H33 | WS 15-min re-auth timer; missing `{"token": …}` frame closes the socket (4401). |
| H34 | Added thread-safe `MQTTClient.publish()` — device config-push was dead code at runtime. |
| H35 | PM10 Hazardous band floor corrected 301→401 (was under-reporting AQI by ~55). |
| H36 | Per-host MQTT client id (`MQTT_CLIENT_ID` or hostname-derived) — ends broker takeover storms. ⚠ Keep one client per host. |

---

## 🟡 MEDIUM (all findings)

### Circuit breaker & Celery (`celery_app.py`)

| # | Fix |
|---|-----|
| M1 | Real rolling window: per-task sorted set pruned by `ZREMRANGEBYSCORE` over 300 s. |
| M2 | Prune+count / prune+add are single atomic Lua scripts (no interleave race). |
| M3 | `retrain_model` renamed under the `empyrean.` prefix in decorator + beat schedule. |
| M6 | Breaker enforced in a shared `CircuitBreakerTask` base — `.delay()`/`apply_async`/`send_task`/beat all gated. |
| M79 | `refresh_token_cleanup` actually scheduled in beat (daily 03:41). |
| M80 | `reset_circuit_breaker()` SCANs the key prefix — bare-key DELETE was a silent no-op. |
| M81 | FAILURE *and* RETRY record failures; check path never feeds the window (breaker can now open). |
| M82 | `delay/apply_async` mirror Celery's signature; open circuit raises exported `CircuitBreakerOpenError`. |

### Auth, rate limit, admin (`api/`)

| # | Fix |
|---|-----|
| M8 | Token issue joins the caller's transaction — one session per login, not two. |
| M12 | ~30 s Redis user cache on the auth path; invalidated on is_active/role mutations. |
| M13+M90 | Settings PATCH writes `AuditLog` rows; migration `0005` creates the missing `audit_logs` table. |
| M14 | Threshold PATCH validates the **merged** DB/config+proposed state, defensive int parsing. |
| M15 | Documented: `Bearer` is the only supported auth scheme (RFC 6750). |
| M16 | Documented: buckets key on view-function name, not path (bounded by design). |
| M17 | Dead in-memory fallback dict deleted; limiter honestly fails open, bypass counter surfaced. |
| M19 | Fail-open responses labelled `X-RateLimit-Bypass: true`. |
| M21 | Inverted threshold pairs rejected at request time *and* via merged-state check. |
| M84 | Settings PATCH rejects explicit `null` (422); garbage stored values can't 500 the merge. |

### Cache, readings, nodes, alerts, profile, export, forecast (`api/`)

| # | Fix |
|---|-----|
| M18 | Cache client self-heals — failed build retries next call (mirrors `tasks/_redis.py`). |
| M22 | `reset_cache_client()` hook, wired into `conftest.py`. |
| M24 | Clamped history ranges set `X-Range-Clamped` header. |
| M25 | Re-registering a deactivated node undeletes it (upsert); active dupes still 409. |
| M26 | Deactivation pushes a `disabled` MQTT config so the device stops publishing. |
| M27 | Alert severity filter + pagination pushed into SQL (no more full-list loads). |
| M29 | Forecast import catch broadened to `ImportError`. |
| M30 | `import time` moved to module top in `api/export.py`. |
| M31 | `MAX_EXPORT_TIMEOUT` → `EXPORT_TIMEOUT_SECONDS` config. |
| M32 | Profile pydantic dump cached on `g` per request. |
| M35 | Empty `?node_id=` → 422 instead of silently meaning "all nodes". |
| M83 | Timeout-cut CSV exports emit a sentinel trailer row. |
| M85 | Dead double-bcrypt writes removed from `change_password`/`delete_profile`. |
| M86 | Profile update reuses registration schema validators. |
| M50 | Forecast cache versioned by model `trained_at` (`celery:forecast:{node}:{version}`) — retrain race closed. |

### MQTT (`mqtt/`)

| # | Fix |
|---|-----|
| M36 | Readiness tracked by granted topic set, not SUBACK counter. |
| M37 | `queue.Full` drops increment the overflow counter + log. |
| M38 | Dropped-reading/overflow counters wired into `/admin/health`. |
| M39 | Per-node inbound rate bound on invalid status payloads. |
| M40 | Per-worker retry deque documented (deliberately uncoordinated). |
| M41 | Publisher uses `connect_async` + `loop_start` (no blocking handshake). |
| M88 | Readiness/subscription state reset on disconnect. |

### Tasks (`tasks/`)

| # | Fix |
|---|-----|
| M43 | Anomaly subquery explicitly aliased (`"recent"`). |
| M44 | Aggregate names no longer shadow builtins. |
| M48 | Keep-last-N token carve-out bounded to 30 days post-expiry. |
| M49 | Retrain loads fleet training points in ONE query/session. |
| M52 | One SMTP connection per beat run for the whole alert batch. |
| M53 | AQI truncated (`math.floor`) per EPA — no more off-by-one at edges. |
| M55 | Dead `_ANOMALY_WINDOW_HOURS` deleted. |

### Models, config, migrations, ops

| # | Fix |
|---|-----|
| M4 | Tracing moved to `app_factory/tracing.py`; root `app/` shadow deleted; plain package imports. |
| M5 | 64 KB body cap → `MAX_CONTENT_LENGTH` config. |
| M56 | Dead duplicate `models/base_new.py` deleted. |
| M57 | `reset_engines()` wired into `reset_config_cache()`. |
| M58 | Engine aliases rebound on every reinit. |
| M62 | `alert_email` capped at 255 chars in the schema. |
| M63 | `token_hash` → `String(64)` (migration `0006`). |
| M64 | Partial `(triggered_at DESC) WHERE unacked` index (migration `0006`). |
| M67 | Secret-strength check relaxed behind explicit `APP_ENV=test` (blocklist still global). |
| M68 | TLS validator requires `MQTT_CA_CERTS` too. |
| M69 | `scripts/` is now a package. |
| M71 | celery-beat unit guarded with `flock --nonblock`. |
| M72 | Deploy order: stop → migrate → restart. |
| M73 | `DATABASE_URL` with empty username rejected. |
| M74 | Dead WS loop marked stale on `RuntimeError`; next connect recaptures. |
| M75 | WS frame-size check covers decoded (non-str) frames via JSON length. |
| M87 | WS broadcast gathered concurrently (`return_exceptions=True`). |
| M89 | Reinit disposes sync+async engines; `dispose_engines()` clears state + rebinds aliases. |

### Pinning tests added

| # | Test |
|---|------|
| M23 | Concurrent requests never cross start times (`test_request_logging`). |
| M33 | Unmatched routes label `route="unknown"` — bounded cardinality (`test_metrics`). |
| M45 | Decimal COUNT tolerated by the anomaly guard (`test_process_reading`). |
| M46 | `hourly_aggregate` idempotent across watermark re-runs (`test_aggregation`). |
| M54 | AQI band-edge truncation + PM10 gap behaviour pinned (`test_aqi`). |

---

## 🟢 LOW (all findings)

| # | Fix |
|---|-----|
| L1 | No-op `teardown_request` hook removed. |
| L2 | Unused `cfg`/`app_logger` params dropped from lifecycle registrars. |
| L3 | `_problem_json` made public (`problem_json`) across 12 importers. |
| L6 | Dead `bind_request_session`/`get_request_session` `g` indirection removed. |
| L8 | `_load_settings` single-pass over one merged key list. |
| L9 | `alert_email` gained its `ALERT_EMAIL` config fallback. |
| L12 | Password cap → `PASSWORD_MAX_BYTES` config field. |
| L13 | `lat`/`lon` range-checked (±90 / ±180). |
| L14 | `validated_body()` raises when the route lacks `@validate_body`. |
| L15 | Cache config resolved per call (no stale module snapshot). |
| L17 | Forecast cache-key templates in shared helpers (both sides). |
| L18 | CSV header rendered once via helper. |
| L21 | Publisher-local retry constants documented as deliberate. |
| L22 | Topic template + node-id regex centralized in one helper. |
| L23 | `_pending_subs` cleared on disconnect. |
| L24 | Publisher stats wired into `/admin/health`. |
| L25 | `CHECK (LENGTH(message) <= 10000)` on alerts (migration `0006`). |
| L26 | TTL race subsumed by M50's versioned keys. |
| L29 | `dispose_engines` re-exported from `models`. |
| L30 | `prepared_statement_cache_size=0` rationale documented (pgbouncer). |
| L31 | Dead `get_request_session` removed from `models/helpers`. |
| L32 | Duplicate `import logging` removed. |
| L33 | `_raw_app_env` hand-rolled `.env` parser → pydantic `model_validator(mode="before")`. |
| L34 | `scripts/seed.py` modernized to 2.x `select()`. |
| L36 | nginx `/metrics` dual gating documented. |
| L37 | Canonical fuzzy import documented (`from fuzzy import …`). |
| L38/L39 | Package docstrings added to empty `__init__.py` files. |
| L41 | Dead `_sanitize` deleted from `api/metrics`. |
| L42 | Rate-limit availability/bypass wired into `/admin/health`; docs corrected. |
| L43 | Redundant second revocation UPDATE removed from `refresh()`. |
| L44 | `_refresh_expiry` truncates to the second, matching its contract. |
| L45 | Cached forecasts shape-checked; corrupted blobs recompute instead of 500. |
| L46 | `REDIS_URL` accepts `rediss://` and `unix://` too. |
| L47 | Dead deps removed (`quart-schema`, `pandas`, `python-dotenv`). |
| L48 | Retention purge deletes stale `hourly_agg` buckets too. |
| L49 | nginx `/ws` sets Host/X-Real-IP/X-Forwarded-* explicitly. |
| L50 | Deploy restarts services around migrations (with M72). |
| L5/L16/L20/L40 | Cosmetic pass: dead comment/casts already clean; metrics cardinality caveat + docstrings tidied. |

---

## ⚪ Closed without code change (verified / accepted / documented)

- **Verified OK:** M20 (single node-id regex), M42 (broadcast capture), M47 (interval bind param), M65 (ORM supplies password_hash), M70 (check_health exit codes), M77 (27-rule cost), L4 (autoretry applied), L19 (redacting `__repr__`), H27-class checks.
- **Accepted (1-min beat window):** M34 (`alerts_enabled` per run), M51 (alert-email per run).
- **Documented in code:** M28 (forecast sync slot), M59/M60 (model↔migration mirror risk — CI gate needs a pipeline first), M61 (`passive_deletes` staleness), L28 (setex/delete window ≤ TTL).
- **Covered by existing tests:** M76 (degenerate shoulder), M78 (27-rule Cartesian product).
- **Resolved elsewhere:** L7 (by M4), O6 (by M7), O3 (logging one-shot).

---

## Follow-ups

- Rotate/remove the old `Darshan` credentials in existing DBs (H5).
- Frontends reading `environment` from `/health` must switch to `/admin/health` (H3).
- Full-suite verification run still pending by agreement.
