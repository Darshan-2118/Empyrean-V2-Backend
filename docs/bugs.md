# Codebase Audit — Open Findings

> Comprehensive audit of the Empyrean V2 backend. Evidence-based with file
> paths and line ranges; findings are grouped by severity, not by audited
> surface:
> - **HIGH** — security or data-loss issue exploitable today (all 20 fixed,
>   see [`docs/fixed_bugs.md`](fixed_bugs.md))
> - **MEDIUM** — correctness bug, race, or behaviour that will bite under load
>   (all resolved — code fixes, tests, or documented no-action)
> - **LOW** — dead code, smell, or hardening that costs little
>
> **This document lists OPEN findings only.** Every HIGH, MEDIUM, and LOW
> finding is now closed; the fix history lives in
> [`docs/fixed_bugs.md`](fixed_bugs.md). IDs are stable so entries stay
> traceable across both files; fixed IDs keep their numbers even though their
> rows move to the fix log. What remains here are standing operational
> advisories, not code defects.

---

## 🟡 Medium / Correctness

**None open.** All 88 MEDIUM findings are resolved (fix log: waves #3–#5 in
[`docs/fixed_bugs.md`](fixed_bugs.md)).

---

## 🟢 Low / Dead Code & Cleanup

**None open.** All LOW findings are resolved — the bulk in the wave #5 +
LOW sweep, the final seven cosmetic items (L5, L16, L20, L33, L36, L37, L40)
in the cosmetic pass. See [`docs/fixed_bugs.md`](fixed_bugs.md).

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

## Summary

| Severity | Count |
|---|---|
| HIGH    | 0 |
| MEDIUM  | 0 |
| LOW     | 0 |
| Ops advisories | 4 |
| **Total open** | **4** |

> All 20 HIGH findings (two waves), all MEDIUM findings (waves #3–#5), and
> all LOW findings (sweep + cosmetic pass) are fixed — see
> [`docs/fixed_bugs.md`](fixed_bugs.md). O1–O5 are standing operational
> notes (O3/O6 closed — O6 by M7's one-shot logging). Full test-suite run
> still pending by agreement.
