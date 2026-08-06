"""Membership functions for the Tsukamoto fuzzy engine.

Each input variable (temperature, humidity, PM2.5) is described by three
linguistic terms.  Inputs are clamped to the variable's domain before a
membership value is computed; points outside a term's support return 0.
"""

from __future__ import annotations

from typing import Callable

# Inclusive input domains, used to clamp out-of-range sensor readings.
T_DOMAIN: tuple[float, float] = (0.0, 50.0)
H_DOMAIN: tuple[float, float] = (0.0, 100.0)
P_DOMAIN: tuple[float, float] = (0.0, 500.0)


def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp ``x`` to the inclusive interval ``[lo, hi]``."""
    return max(lo, min(hi, x))


def tri(x: float, a: float, b: float, c: float) -> float:
    """Triangular membership: 1 at ``b``, 0 at the support edges ``a``/``c``.

    Returns 0 for any ``x`` outside the closed interval ``[a, c]``.  A
    degenerate triangle (``a == b``) is treated as a left shoulder — the peak
    plateau is flush with the coincident edge, mirroring ``trap()``'s shoulder
    guard — so it never divides by zero.
    """
    if x <= a or x >= c:
        return 0.0
    if b == a:  # left shoulder — peak flush with edge ``a``
        return 1.0
    if x <= b:
        return (x - a) / (b - a)
    return (c - x) / (c - b)


def trap(x: float, a: float, b: float, c: float, d: float) -> float:
    """Trapezoidal membership with plateau ``[b, c]`` and linear ramps.

    Rises from 0 at ``a`` to 1 at ``b``, holds 1 until ``c``, then falls to 0
    at ``d``.  A shoulder (``a == b`` or ``c == d``) keeps the plateau flush
    with that boundary, so a left shoulder is 1 from the domain edge up to
    ``c``.
    """
    if b <= x <= c:
        return 1.0
    if x < b:
        if b == a:
            return 1.0
        if x <= a:
            return 0.0
        return (x - a) / (b - a)
    # x > c
    if c == d:
        return 1.0
    if x >= d:
        return 0.0
    return (d - x) / (d - c)


# ── Temperature (0–50 °C) ─────────────────────────────────────────────────────
# The three terms form a proper partition of the domain: adjacent terms cross
# at 0.5 and the memberships sum to 1 everywhere (see tests/test_fuzzy.py).

def temp_low(t: float) -> float:
    """Low temperature: shoulder 1 on [0, 15] °C, falls to 0 at 35."""
    t = _clamp(t, *T_DOMAIN)
    return trap(t, 0.0, 0.0, 15.0, 35.0)


def temp_medium(t: float) -> float:
    """Medium temperature: rises 15 → 35 °C, flat to 40, falls to 0 at 50."""
    t = _clamp(t, *T_DOMAIN)
    return trap(t, 15.0, 35.0, 40.0, 50.0)


def temp_high(t: float) -> float:
    """High temperature: rising ramp 40 → 50 °C, then 1 up to the domain top."""
    t = _clamp(t, *T_DOMAIN)
    return trap(t, 40.0, 50.0, 50.0, 50.0)


# ── Humidity (0–100 %) ────────────────────────────────────────────────────────

def hum_dry(h: float) -> float:
    """Dry air: shoulder 1 on [0, 30] %, falls to 0 at 70 %."""
    h = _clamp(h, *H_DOMAIN)
    return trap(h, 0.0, 0.0, 30.0, 70.0)


def hum_humid(h: float) -> float:
    """Humid air: rises 30 → 70 %, flat to 80, falls to 0 at 100 %."""
    h = _clamp(h, *H_DOMAIN)
    return trap(h, 30.0, 70.0, 80.0, 100.0)


def hum_wet(h: float) -> float:
    """Wet air: rising ramp 80 → 100 %, then 1 up to the domain top."""
    h = _clamp(h, *H_DOMAIN)
    return trap(h, 80.0, 100.0, 100.0, 100.0)


# ── PM2.5 (0–500 µg/m³) ───────────────────────────────────────────────────────

def pm_low(p: float) -> float:
    """Low pollution: shoulder 1 on [0, 50] µg/m³, falls to 0 at 100."""
    p = _clamp(p, *P_DOMAIN)
    return trap(p, 0.0, 0.0, 50.0, 100.0)


def pm_medium(p: float) -> float:
    """Medium pollution: rises 50 → 100 µg/m³, flat to 200, falls to 0 at 300."""
    p = _clamp(p, *P_DOMAIN)
    return trap(p, 50.0, 100.0, 200.0, 300.0)


def pm_high(p: float) -> float:
    """High pollution: rising ramp 200 → 300 µg/m³, then 1 up to 500."""
    p = _clamp(p, *P_DOMAIN)
    return trap(p, 200.0, 300.0, 500.0, 500.0)


# Term lookups keyed by the linguistic label used in the rule base.
MembershipFn = Callable[[float], float]
TEMP_TERMS: dict[str, MembershipFn] = {"low": temp_low, "medium": temp_medium, "high": temp_high}
HUM_TERMS: dict[str, MembershipFn] = {"dry": hum_dry, "humid": hum_humid, "wet": hum_wet}
PM_TERMS: dict[str, MembershipFn] = {"low": pm_low, "medium": pm_medium, "high": pm_high}
