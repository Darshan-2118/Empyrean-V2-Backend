# Codebase Audit — Open Findings

> Comprehensive audit of the Empyrean V2 backend. Evidence-based with file
> paths and line ranges; findings are grouped by severity, not by audited
> surface:
> - **HIGH** — security or data-loss issue exploitable today
> - **MEDIUM** — correctness bug, race, or behaviour that will bite under load
> - **LOW** — dead code, smell, or hardening that costs little
>
> **This document lists OPEN findings only.** Wave-1 findings (H1–H36,
> M1–M90, L1–L50) and wave-2 findings (H37, M91–M111, L51–L78) are all
> closed; the fix history lives in
> [`docs/FIXED_BUGS.md`](FIXED_BUGS.md). IDs are stable so entries stay
> traceable across both files; fixed IDs keep their numbers even though their
> rows move to the fix log.
>
> **Wave 2 (2026-08-27/28):** full re-audit of the post-fix tree, split into
> six parts — (1) API auth/security, (2) API data endpoints + WebSocket,
> (3) bootstrap/config/Celery, (4) models/migrations/MQTT/fuzzy,
> (5) tasks/scripts/deploy, (6) test suite + dev shell scripts. Each part
> was read line-by-line with the wave-1 fix log excluded. 50 new findings:
> 1 HIGH, 21 MEDIUM, 28 LOW.
>
> **Wave 2 closure (2026-08-28):** all 50 wave-2 findings fixed and verified
> with a full green suite (**326 passed, 0 failed**). Part-6 added the
> test-suite findings (M106–M111, L76–L77 — false-confidence tests, two of
> which were failing against the post-fix production behaviour) and the
> `scripts/db.sh` credential exposure (L78). Rows below moved to
> [`docs/FIXED_BUGS.md`](FIXED_BUGS.md).

---

## 🔴 High / Security

_None open._

---

## 🟡 Medium / Correctness

_None open._

---

## 🟢 Low / Dead Code & Cleanup

_None open._

---

## Cross-Cutting Findings (Deployment / Ops)

> Standing advisories — not code defects.

| # | File | Finding |
|---|------|---------|
| O1 | `.env.example` | Verified: only placeholder values committed; no real `.env` tracked. Git-history scrub optional (BFG) if a real key ever landed there. |
| O2 | `requirements.txt` | Direct deps pinned exactly; transitive deps unpinned — consider a lockfile (`pip-tools` / `uv`). |
| O4 | `tests/` | Fixtures use synthetic users/passwords — OK on inspection. Keep it that way. |
| O5 | All Celery tasks | `name=` ↔ beat entries match, all under the `empyrean.` prefix (M3's fix aligned the last outlier). Re-verify with `celery inspect registered` after any rename. |

---

## Verified clean in wave 2 (explicitly checked, no findings)

- **`api/rate_limit.py`, `api/validation.py`, `api/schemas.py`, `api/_time.py`, `api/request_log.py`** — atomic Lua INCR+PEXPIRE, honest fail-open, capped/sanitised 422 details, correct decorator ordering on all routes, tz-aware expiry math.
- **`api/auth.py` refresh/login** — atomic `UPDATE … RETURNING` claim, reuse-detection chain revocation, timing-equalising dummy bcrypt.
- **SQL injection / CSV injection** — `history()` interpolates only the fixed bucket map into `text()`; all user inputs are bind params; export's only free-text column (`node_id`) is regex-constrained so no formula/DDE prefix is possible.
- **`fuzzy/`** — term partitions sum to 1.0, 27-rule table complete, `total_alpha == 0` guarded, NaN/Inf rejected at the boundary.
- **`tasks/process_reading.py`, `tasks/alerts.py`, `tasks/aqi.py`, `tasks/forecast.py`, `tasks/_redis.py`** — clamp/anomaly/dedup/upsert/versioned-cache/one-SMTP-connection fixes all verified in place.
- **Error handlers & middleware** (`app_factory/helpers.py`) — full status coverage incl. 413 and catch-all, problem+json only, no HTML 500 leak; CORS/preflight/logging ordering sound.

---

## Summary

| Severity | Count |
|---|---|
| HIGH    | 0 |
| MEDIUM  | 0 |
| LOW     | 0 |
| Ops advisories | 4 (standing, not code defects) |
| **Total open** | **0** |

> Wave 1 (H1–H36, M1–M90, L1–L50) and wave 2 (H37, M91–M111, L51–L78) are
> fully closed — see [`docs/FIXED_BUGS.md`](FIXED_BUGS.md). Final suite:
> **326 passed, 0 failed** (2026-08-28).
