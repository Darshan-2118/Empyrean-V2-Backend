# Empyrean — Tsukamoto Fuzzy Inference Engine

This document describes the Tsukamoto fuzzy inference engine that powers the AQI scoring. It covers the defuzzification formula, the input variables, and the sample rule base.

The AQI fuzzy engine consumes three input variables and outputs a crisp 0–100 score via weighted-average defuzzification: `AQI_crisp = Σ(αᵢ × zᵢ) / Σ(αᵢ)`, where `αᵢ` is each rule's firing strength (min of antecedent memberships) and `zᵢ` is the crisp output from the consequent's monotonic function.

**Input variables:**

| Variable | Range | Fuzzy Terms |
|---|---|---|
| Temperature (T) | 0–50 °C | Low (0–30), Medium (25–35–45), High (40–50) |
| Humidity (H) | 0–100 % | Dry (0–50), Humid (40–60–80), Wet (70–100) |
| PM2.5 Pollution (P) | 0–500 µg/m³ | Low (0–50), Medium (40–60–80), High (70–100+) |

**Rule base (sample — 6 of 27 possible combinations):**

| Rule | Condition | Output |
|---|---|---|
| R1 | T=Medium AND P=Medium AND H=Humid | AQI = Medium |
| R2 | T=High AND P=High | AQI = Bad |
| R3 | T=Low AND P=Low AND H=Wet | AQI = Good |
| R4 | T=High AND P=Medium | AQI = Medium |
| R5 | T=Low AND P=High | AQI = Bad |
| R6 | T=Medium AND P=Low | AQI = Good |

> With 3 variables × 3 terms there are up to 27 possible rule combinations. The table above is illustrative only — the full rule base needs to be finalized in `fuzzy/tsukamoto.py` before the engine is actually implementable; treat this as an open task, not a spec.

## Related Docs

- [architecture.md](architecture.md)
- [TODO.md](TODO.md)
- [README](../README.md)
