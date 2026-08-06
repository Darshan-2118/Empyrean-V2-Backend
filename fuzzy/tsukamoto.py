"""Tsukamoto defuzzification and the public ``infer`` entrypoint.

The engine maps temperature, humidity and PM2.5 to a crisp AQI-like score in
[0, 100].  For every fired rule the consequent's monotonic ramp is inverted at
the rule's firing strength to give a crisp output ``z``; the final score is the
firing-strength-weighted average of those ``z`` values.
"""

from __future__ import annotations

import math

from fuzzy.membership import HUM_TERMS, PM_TERMS, TEMP_TERMS
from fuzzy.rules import CONSEQUENTS, RULE_TABLE

# Fallback score returned when no rule fires.  Defensive only: the term
# partitions cover each input domain exactly (adjacent terms cross at 0.5 and
# the memberships sum to 1), so every in-domain reading fires at least one rule.
DEFAULT_SCORE = 20.0

# Inputs that are not finite numbers are rejected at the infer() boundary so a
# NaN/Inf reading can never silently clamp to a domain top (see docs/known-issues.md M-2).
_NON_FINITE_MSG = "{name} must be a finite number, got {value!r}"

# A fired rule: (firing strength alpha, crisp output z).
_Fired = tuple[float, float]


def _inverse(alpha: float, lo: float, hi: float) -> float:
    """Invert a monotonic rising ramp: membership ``alpha`` -> output ``z``.

    A linear ramp from ``lo`` (membership 0) to ``hi`` (membership 1) has the
    inverse ``z(alpha) = lo + alpha * (hi - lo)``.
    """
    return lo + alpha * (hi - lo)


def _weighted_average(fired: list[_Fired]) -> float:
    """Tsukamoto weighted average over fired ``(alpha, z)`` pairs.

    Returns :data:`DEFAULT_SCORE` when nothing fired (``sum(alpha) == 0``).
    """
    total_alpha = sum(alpha for alpha, _ in fired)
    if total_alpha == 0:
        return DEFAULT_SCORE
    return sum(alpha * z for alpha, z in fired) / total_alpha


def _require_finite(value, name: str) -> None:
    """Reject non-numeric / NaN / Inf inputs at the public boundary."""
    if value is None or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(_NON_FINITE_MSG.format(name=name, value=value))


def infer(temperature: float, humidity: float, pm25: float) -> dict:
    """Compute a crisp fuzzy score for one reading.

    Rejects ``None``/non-numeric and non-finite (NaN/Inf) inputs with a
    ``ValueError`` so bad sensor data cannot silently clamp to a domain top
    (M-2).  Returns ``{"score": float, "rules_fired": int}`` where ``score`` is
    in [0, 100] rounded to two decimals.
    """
    _require_finite(temperature, "temperature")
    _require_finite(humidity, "humidity")
    _require_finite(pm25, "pm25")

    mu_t = {name: fn(temperature) for name, fn in TEMP_TERMS.items()}
    mu_h = {name: fn(humidity) for name, fn in HUM_TERMS.items()}
    mu_p = {name: fn(pm25) for name, fn in PM_TERMS.items()}

    fired: list[_Fired] = []
    for t_name, t_mu in mu_t.items():
        if t_mu == 0:
            continue
        for h_name, h_mu in mu_h.items():
            if h_mu == 0:
                continue
            for p_name, p_mu in mu_p.items():
                if p_mu == 0:
                    continue
                alpha = min(t_mu, h_mu, p_mu)
                if alpha == 0:
                    continue
                lo, hi = CONSEQUENTS[RULE_TABLE[(t_name, h_name, p_name)]]
                fired.append((alpha, _inverse(alpha, lo, hi)))

    score = _weighted_average(fired)
    return {"score": round(score, 2), "rules_fired": len(fired)}


def fuzzy_score(temperature: float, humidity: float, pm25: float) -> float:
    """Convenience wrapper returning just the crisp score from :func:`infer`."""
    return infer(temperature, humidity, pm25)["score"]
