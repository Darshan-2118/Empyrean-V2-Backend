"""EPA AQI computation from PM2.5 / PM10.

Design notes:
    * Pure function — returns None when inputs are invalid.
    * Logs NaN/Inf/None in concentrations to surface bad sensor readings.
    * Pure math - no Celery, no DB, so it is trivially unit-testable.

Live sensor concentrations are used here as an *instantaneous proxy* for the
EPA 24-hour averages (the standard AQI contract): this trades strict regulatory
accuracy for real-time responsiveness, which is what a live air-quality map
needs. See the p7 report for the note on this simplification.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger("empyrean.tasks.aqi")

# (C_lo, C_hi, I_lo, I_hi) breakpoints, ascending, the full 7-row EPA tables.
# The Hazardous range keeps EPA's two sub-bands (301–400 / 401–500) so AQI is
# not under-reported in the upper concentration range (known-issues L-4).
_PM25_BANDS = [
    (0.0, 12.0, 0, 50),
    (12.0, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
]

_PM10_BANDS = [
    (0.0, 54.0, 0, 50),
    (55.0, 154.0, 51, 100),
    (155.0, 254.0, 101, 150),
    (255.0, 354.0, 151, 200),
    (355.0, 424.0, 201, 300),
    (425.0, 504.0, 301, 400),
    # H35: EPA's PM10 Hazardous sub-band is AQI 401–500 (mirroring the PM2.5
    # table above). The old I_lo=301 under-reported the whole 505–604 range by
    # up to ~55 index points, masking genuine Hazardous events below the
    # critical-alert threshold.
    (505.0, 604.0, 401, 500),
]

# Mirrors the band ordering above (index by band position).
CATEGORIES = [
    "Good",
    "Moderate",
    "Unhealthy for Sensitive Groups",
    "Unhealthy",
    "Very Unhealthy",
    "Hazardous",
    "Hazardous",
]


def _subindex(concentration: float, bands: list[tuple]) -> int:
    """Index of the band containing ``concentration`` (clamped to the top band)."""
    for i, band in enumerate(bands):
        if concentration <= band[1]:  # band[1] is the C_hi breakpoint
            return i
    return len(bands) - 1


def _aqi_for(concentration: float | None, bands: list[tuple]) -> tuple[int | None, str | None]:
    """AQI + category for one pollutant via linear interpolation within its band.

    Returns ``(None, None)`` when ``concentration`` is ``None`` or non-finite.
    The concentration is clamped to ``[0, top C_hi]`` before the band lookup so
    a negative concentration maps to the Good band (AQI 0, not negative - L-13)
    and a value above the last breakpoint cannot extrapolate past 500 (H-2).
    The fractional result is truncated (``math.floor``), matching the EPA
    convention — the old round-half-up produced off-by-one AQI values at
    breakpoint edges (M53).
    """
    if concentration is None or not math.isfinite(concentration):
        return None, None
    top_hi = bands[-1][1]
    c = max(0.0, min(concentration, top_hi))
    i = _subindex(c, bands)
    lo, hi, ilo, ihi = bands[i]
    aqi = math.floor((ihi - ilo) / (hi - lo) * (c - lo) + ilo)
    return aqi, CATEGORIES[i]


def compute_aqi(pm25: float | None, pm10: float | None) -> tuple[int | None, str | None]:
    """Return the composite (aqi, category).

    Uses the EPA breakpoints for each pollutant, then takes the *max* AQI of the
    two. Returns (None, None) when both pollutants are missing.

    Logs NaN/Inf/None in concentrations to surface bad sensor readings (#11).
    """
    if pm25 is None and pm10 is None:
        return None, None

    candidates: list[tuple[int, str]] = []
    if pm25 is not None:
        if not math.isfinite(pm25):
            logger.warning("Invalid PM2.5 concentration: %r (not finite)", pm25)
        else:
            candidates.append(_aqi_for(pm25, _PM25_BANDS))
    if pm10 is not None:
        if not math.isfinite(pm10):
            logger.warning("Invalid PM10 concentration: %r (not finite)", pm10)
        else:
            candidates.append(_aqi_for(pm10, _PM10_BANDS))

    if not candidates:
        return None, None

    return max(candidates, key=lambda c: c[0])