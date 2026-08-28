# Fixed Bugs Log

> Every fix from the audit in [`docs/bugs.md`](bugs.md), grouped by severity.
> IDs are stable for traceability; full detail lives in git history. Wave 1
> landed 2026-08-25 → 2026-08-27 across six waves (HIGH A-2, MEDIUM A-3,
> cosmetic pass); wave 2 (H37, M91–M111, L51–L78) landed 2026-08-28.

---

## Wave 2 — 2026-08-28 (H37, M91–M111, L51–L78)

### 🔴 HIGH

| # | Fix |
|---|-----|
| H37 | `change_password` verifies against a hash fetched fresh from the DB (never the cached user) and invalidates the Redis user cache right after the commit; `password_hash` is no longer cached at all (`api/profile.py`, `api/jwt.py`). |

### 🟡 MEDIUM

| # | Fix |
|---|-----|
| M91 | Bootstrap-admin provisioning matches the username exactly; a case-variant account is logged and refused, never promoted (`api/auth.py`). |
| M92 | `/profile/change-password` gets `rate_limit(10, 60)` per IP, mirroring `/login` (`api/profile.py`). |
| M93 | WS re-auth window computed from the last *successful* auth — any frame no longer restarts the timer; deadline closes 4401 (`api/ws/routes.py`). |
| M94 | WS handshake **and** re-auth check the jti blocklist via `_is_token_revoked`, mirroring the REST path (`api/ws/routes.py`). |
| M95 | `/readings/history` caps grouped rows (`_HISTORY_MAX_ROWS = 50_000`, LIMIT cap+1 probe) → 422 with a narrow-range/pass-node_id hint on overflow (`api/readings.py`). |
| M96 | `task_acks_on_failure_or_timeout=True` + honest at-most-once comments — ends both the silent reject-and-lose path and the hourly `visibility_timeout` redelivery loop (`celery_app.py`). |
| M97 | `DATABASE_URL` scheme allowlist is now exactly `postgresql` / `postgres` / `postgresql+psycopg2` — the installed driver (`config`). |
| M98 | `> 0` validators for `JWT_ACCESS_TOKEN_EXPIRY_MINUTES`, `JWT_REFRESH_TOKEN_EXPIRY_DAYS`, `EXPORT_TIMEOUT_SECONDS`, `MQTT_QUEUE_MAX` (`config`). |
| M99 | Alert-publisher client id suffixed `-{hostname}-{pid}` (H36 pattern) — prefork workers no longer takeover each other's broker session (`mqtt/publisher.py`). |
| M100 | Publisher retry never sleeps in the caller's thread, processes a bounded batch (10) per call, and re-enqueues everything not published via `try/finally` — an interrupted batch can't lose messages (`mqtt/publisher.py`). |
| M101 | Per-node rate limiter exempts replayed backlog (device timestamps ≥ 60 s old) so the post-reconnect QoS1 burst isn't dropped at ingest (`mqtt/client.py`). |
| M102 | `_node_last_seen` bounded (10k cap, oldest-first eviction) — random node ids can't grow it without limit (`mqtt/client.py`). |
| M103 | celery-beat's unprivileged `ExecStartPre` replaced with `RuntimeDirectory=empyrean` — the unit can actually start on a prod host (`deploy/celery-beat.service`). |
| M104 | rsync excludes `.celery` and `.venv` — dev beat schedule DB and local site-packages no longer ship to prod (`deploy/deploy.sh`). |
| M105 | Dedicated nginx `location /api/v1/export` with `proxy_read_timeout 330s` so 300 s CSV exports aren't 504'd (`deploy/nginx.conf`). |
| M106 | Beat-schedule guard updated to the real five entries incl. `refresh-token-cleanup` — was failing today against the M79 schedule (`tests/test_celery_app.py`). |
| M107 | Client-ID test updated to the H36 contract (prefix + hostname suffix) — was failing today, pinning the pre-H36 fixed id (`tests/mqtt/reconnection_test.py`). |
| M108 | Reconnect-delay test asserts the real paho min/max (1/60) — the old `... or True` could never fail (`tests/mqtt/reconnection_test.py`). |
| M109 | SUBACK test asserts granted topics, consumed mids, and readiness flipping — the old assertion was tautological (`tests/mqtt/reconnection_test.py`). |
| M110 | Heartbeat test now exercises the REAL `_handle_status` against the DB and asserts `Node.last_seen` moves — the old test called a mock and asserted the mock was called (`tests/mqtt/reconnection_test.py`). |
| M111 | Full-queue test asserts the drop, the warning, and the overflow counter increment — the old test had no assertions (`tests/mqtt/reconnection_test.py`). |

### 🟢 LOW

| # | Fix |
|---|-----|
| L51 | Blocklist is now per-jti keys (`SET jwt:blocklist:{jti} 1 EX ttl` — self-cleaning); logout also revokes the presented access token, and the docstrings now tell the truth (`api/jwt.py`, `api/auth.py`). |
| L52 | Module-level config snapshots in `api/jwt.py` and `api/admin.py` resolved per call — no stale secret/expiry after `reset_config_cache()`. |
| L53 | Redis URL logged with credentials redacted (`api/cache.py`). |
| L54 | Settings PATCH re-validates the merged threshold pair under `SELECT … FOR UPDATE` inside the write transaction — concurrent patches can't commit an inverted pair (`api/admin.py`). |
| L55 | `METRICS_SECRET` compared with `hmac.compare_digest` (`api/metrics.py`). |
| L56 | Empty/whitespace `?node_id=` on export → 422, mirroring `/readings/history` (`api/export.py`). |
| L57 | Dead `_validate_handshake_token` deleted (`api/ws/routes.py`). |
| L58 | Engine-init debug log renders the DB URL with `hide_password=True` (`models/base.py`). |
| L59 | `shutdown_tracing()` idempotent and registered as an `after_serving` hook when tracing is on — spans flush on SIGTERM (`app_factory/tracing.py`, `factory.py`). |
| L60 | `.celery/` mkdir wrapped in `try/except OSError` — importing `celery_app` survives a read-only rootfs. |
| L61 | `task_ignore_result=True` and the result backend dropped — nothing reads results (`celery_app.py`). |
| L62 | Fleet retraining offset to `crontab(minute=37)` — no more hourly collision with aggregation (`celery_app.py`). |
| L63 | Broker startup retries raised to 30 with an honest comment: bounded retries, then the process exits and systemd restarts it (`celery_app.py`). |
| L64 | `.env.example` drift closed: `MQTT_ENABLED`, `MQTT_CLIENT_ID`, `MQTT_QUEUE_MAX`, `PASSWORD_MAX_BYTES`, `EXPORT_TIMEOUT_SECONDS`, `SMTP_*`, `ALERT_EMAIL`, `TASK_SOFT/HARD_TIME_LIMIT` added with safe defaults. |
| L65 | MQTT lifecycle teardown registered before DB/Redis teardown — the producer stops first (`app_factory/factory.py`). |
| L66 | Non-empty `BOOTSTRAP_ADMIN_PASSWORD` goes through the weak-secret gate (blocklist always, strength unless `APP_ENV=test`) (`config`). |
| L68 | Rollback-failure path raises `rollback_error from original_exc` — the causing exception is no longer suppressed (`models/base.py`). |
| L69 | Failed `subscribe()` sets a flag and a throttled retry re-subscribes — no more connected-but-never-ready ingestion (`mqtt/client.py`). |
| L70 | 64 KB inbound payload cap before decode/enqueue; `firmware` bounded to 64 chars in `StatusPayload` (`mqtt/client.py`, `mqtt/validator.py`). |
| L71 | Hourly aggregation starts at `watermark − 24h` so late device timestamps (H25 window) fold into closed hours via the idempotent UPSERT (`tasks/aggregation.py`). |
| L72 | `check_health` redacts `REDIS_URL` in both the success detail and the exception branch (`scripts/check_health.py`). |
| L73 | `seed.py` bootstrap-admin lookup uses exact match; case-variant rows are logged and refused — same semantics as M91 (`scripts/seed.py`). |
| L74 | All three systemd units get `StartLimitIntervalSec=300` + `StartLimitBurst=5` — persistent boot failures reach a terminal `failed` state (`deploy/*.service`). |
| L75 | `deploy.sh` fails loudly (stderr + `exit 1`) when `envsubst` is missing instead of printing SUCCESS with the nginx config never installed. |
| L76 | Disconnect/restart lifecycle tests assert real state (readiness/subscription reset; duplicate `start()` is a no-op — guarded in `mqtt/client.py` so a second start no longer leaks a worker thread). |
| L77 | Payload-truncation test asserts the log carries the truncated repr and never the full payload — the old test had no assertions (`tests/mqtt/reconnection_test.py`). |
| L78 | `db.sh` extracts the password into `PGPASSWORD` and passes a sanitised URL to `psql` — the connection string (with password) no longer sits on argv visible via `ps`. |

### Wave-2 verification & test-infrastructure repairs

- Migration `0007` created for L67 (`refresh_tokens.expires_at` index; model stayed source of truth).
- Full suite green after all wave-2 fixes: **326 passed, 0 failed** (2026-08-28).
- 32 pre-existing Windows test failures repaired along the way (not audit findings): async engine forced to `NullPool` in tests + per-test async-pool/Redis isolation in `tests/conftest.py` (asyncpg/redis connections are loop-bound and died across per-test `asyncio.run` loops), and the stale pre-H36 client-id assertion in `tests/mqtt/reconnection_test.py` updated to the hostname-suffixed contract.

---

## Wave 1 — HIGH (all 20 findings)

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

## Wave 1 — 🟡 MEDIUM (all findings)

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

## Wave 1 — 🟢 LOW (all findings)

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
- ~~Full-suite verification run still pending by agreement.~~ Done 2026-08-28: 315 passed, 0 failed (non-integration), after wave-2 fixes + test-infra repairs.
