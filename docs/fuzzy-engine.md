# Empyrean — Tsukamoto Fuzzy Inference Engine

The Tsukamoto fuzzy engine computes a crisp AQI-like score in **[0, 100]** from
temperature, humidity, and PM2.5. The pipeline is composed of three modules under
`fuzzy/`:

| Module | Responsibility |
|---|---|
| `fuzzy/membership.py` | Membership functions for each linguistic term |
| `fuzzy/rules.py` | The full 27-rule rule base + monotonic consequent ramps |
| `fuzzy/tsukamoto.py` | Defuzzification (weighted average) + the `infer()` / `fuzzy_score()` entrypoints |

It is a pure module — no DB, Redis, or network. The Celery reader task
(`tasks/process_reading.py`) consumes it via
`from fuzzy.tsukamoto import fuzzy_score`.

## Input variables & membership functions

Each input is converted to membership values over its three linguistic terms.
Readings outside a variable's domain are **clamped** to the domain before a
membership is computed.  The three terms of each variable form a proper
**partition** of the domain: adjacent terms cross at membership 0.5 and the
memberships sum to 1 everywhere (a `pytest` test sweeps each domain on a 0.1
grid), so every in-domain reading fires at least one rule. (Pollution is *not*
continuously monotonic in PM2.5 — a known, accepted residual of the Tsukamoto
weighted average; see `docs/known-issues.md` N-10.)

`infer()` rejects non-numeric and non-finite (`NaN`/`±Inf`) inputs with a
`ValueError` at the boundary — bad sensor values can never silently clamp to a
domain top.

| Variable | Domain | Terms & support |
|---|---|---|
| Temperature (T) | 0–50 °C | **Low** (shoulder 1 on [0, 15], → 0 at 35) · **Medium** (15→35 up, 1 on [35, 40], → 0 at 50) · **High** (40→50 up, then 1 to 50) |
| Humidity (H) | 0–100 % | **Dry** (shoulder 1 on [0, 30], → 0 at 70) · **Humid** (30→70 up, 1 on [70, 80], → 0 at 100) · **Wet** (80→100 up, then 1 to 100) |
| PM2.5 (P) | 0–500 µg/m³ | **Low** (shoulder 1 on [0, 50], → 0 at 100) · **Medium** (50→100 up, 1 on [100, 200], → 0 at 300) · **High** (200→300 up, then 1 to 500) |

## Rule base — `fuzzy/rules.py`

The rule antecedent is the triple **(Temperature term, Humidity term, PM2.5 term)**;
every combination maps to a linguistic consequent. With 3 terms × 3 variables there
are **27 rules** in `RULE_TABLE`. The consequents and their monotonic output ramps:

| Consequent | Ramp (crisp 0 → 1 membership) |
|---|---|
| Good | 0–40 |
| Moderate | 30–60 |
| Unhealthy for Sensitive Groups (USG) | 50–75 |
| Unhealthy | 65–90 |
| Very Unhealthy | 85–100 |

Assignments (paralleling the Phase 6 spec), driven hardest by pollution, then
temperature:

- **P = Low** → *Good*, except **H = Dry** or **T = High** → *Moderate*.
- **P = Medium** → *Moderate*, except **H = Dry** or **T = High** → *USG*.
- **P = High** → *Unhealthy*, except **H = Dry and T = High** → *Very Unhealthy*.

Consequent totals across all 27 firms: Good **4**, Moderate **9**, USG **5**,
Unhealthy **8**, Very Unhealthy **1**. (A `pytest` test asserts the table covers
exactly 27 combinations and these exact totals.)

## Defuzzification — `fuzzy/tsukamoto.py`

Tsukamoto defuzzification inverts each fired rule's consequent ramp at its firing
strength, then takes the strength-weighted average:

```
AQI_crisp = Σ(αᵢ × zᵢ) / Σ(αᵢ)
```

- `αᵢ` = rule's firing strength = `min` of the three antecedent memberships (rules
  with `α = 0` are skipped).
- `zᵢ` = crisp output from inverting the consequent's linear ramp:
  `z(α) = lo + α · (hi − lo)`.

The consequent ramps are strictly monotonic and therefore invertible, which is what
Tsukamoto requires.

- `infer(t, h, p) -> {"score": float, "rules_fired": int}` — score in [0, 100],
  rounded to two decimals.  Raises `ValueError` on `None`/non-numeric/non-finite
  inputs.
- `fuzzy_score(t, h, p) -> float` — convenience wrapper returning just the score.
- If **no** rule fires (defensive — the term partitions cover each domain
  exactly, so in-domain readings always fire at least one rule), the engine
  returns `DEFAULT_SCORE = 20.0`.

## Public entrypoints

Re-exported from the `fuzzy` package (`fuzzy/__init__.py`):

```python
from fuzzy import fuzzy_score, infer
fuzzy_score(t=25.0, h=60.0, p=120.0)   # -> float in [0, 100]
infer(25.0, 60.0, 120.0)               # -> {"score": ..., "rules_fired": ...}
```

## Examples

| Input (T, H, P) | Score | Notes |
|---|---|---|
| (20, 50, 10) | 29.6 | clean air — low Good/Moderate ramp weight |
| (20, 50, 250) | 63.4 | heavy pollution — Unhealthy ramps |
| (99, 0, 9999) | 100.0 | saturated Very Unhealthy |

## Related Docs

- [architecture.md](architecture.md)
- [TODO.md](TODO.md)
- [README](../README.md)