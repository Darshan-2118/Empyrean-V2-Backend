"""Rule base for the Tsukamoto fuzzy engine.

27 rules cover every combination of the three temperature, three humidity and
three PM2.5 terms.  Each rule maps an antecedent tuple ``(T, H, P)`` to a
linguistic consequent.  Pollution (PM2.5) drives the score most, temperature
next.

Assignment rules (from the Phase 6 spec):
- P=Low  → Good, except H=Dry → Moderate, and T=High → Moderate.
- P=Med  → Moderate, except H=Dry **or** T=High → UnhealthySensitive.
- P=High → Unhealthy, except H=Dry **and** T=High → VeryUnhealthy.
"""

from __future__ import annotations

# Consequent labels (also the keys of :data:`CONSEQUENTS`).
GOOD = "good"
MODERATE = "moderate"
USG = "usg"  # Unhealthy for Sensitive Groups
UNHEALTHY = "unhealthy"
VERY_UNHEALTHY = "very-unhealthy"

# Monotonic output ramps: (low, high) such that consequent membership 0 -> low
# and 1 -> high.  These are invertible, which Tsukamoto defuzzification needs.
CONSEQUENTS: dict[str, tuple[float, float]] = {
    GOOD: (0.0, 40.0),
    MODERATE: (30.0, 60.0),
    USG: (50.0, 75.0),
    UNHEALTHY: (65.0, 90.0),
    VERY_UNHEALTHY: (85.0, 100.0),
}

# (temperature_term, humidity_term, pm25_term) -> consequent.
RULE_TABLE: dict[tuple[str, str, str], str] = {
    # ── PM2.5 low: Good, bumped to Moderate by dry air or hot weather ──
    ("low", "dry", "low"): MODERATE,
    ("low", "humid", "low"): GOOD,
    ("low", "wet", "low"): GOOD,
    ("medium", "dry", "low"): MODERATE,
    ("medium", "humid", "low"): GOOD,
    ("medium", "wet", "low"): GOOD,
    ("high", "dry", "low"): MODERATE,
    ("high", "humid", "low"): MODERATE,
    ("high", "wet", "low"): MODERATE,
    # ── PM2.5 medium: Moderate, raised to USG by dry air or hot weather ──
    ("low", "dry", "medium"): USG,
    ("low", "humid", "medium"): MODERATE,
    ("low", "wet", "medium"): MODERATE,
    ("medium", "dry", "medium"): USG,
    ("medium", "humid", "medium"): MODERATE,
    ("medium", "wet", "medium"): MODERATE,
    ("high", "dry", "medium"): USG,
    ("high", "humid", "medium"): USG,
    ("high", "wet", "medium"): USG,
    # ── PM2.5 high: Unhealthy, escalating to VeryUnhealthy in hot dry air ──
    ("low", "dry", "high"): UNHEALTHY,
    ("low", "humid", "high"): UNHEALTHY,
    ("low", "wet", "high"): UNHEALTHY,
    ("medium", "dry", "high"): UNHEALTHY,
    ("medium", "humid", "high"): UNHEALTHY,
    ("medium", "wet", "high"): UNHEALTHY,
    ("high", "dry", "high"): VERY_UNHEALTHY,
    ("high", "humid", "high"): UNHEALTHY,
    ("high", "wet", "high"): UNHEALTHY,
}


def consequent_for(t_term: str, h_term: str, p_term: str) -> str:
    """Return the consequent label for a single rule's antecedent terms."""
    return RULE_TABLE[(t_term, h_term, p_term)]