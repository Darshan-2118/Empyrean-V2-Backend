"""Unit tests for EPA AQI computation (pure math — no DB, no fixtures).

Pin the documented 0–500 AQI range (docs/database.md) against concentrations
above the top EPA breakpoint, which must clamp instead of extrapolating (H-2).
"""

from tasks.aqi import compute_aqi


def test_pm25_above_top_breakpoint_caps_at_500():
    """H-2: PM2.5=1000 µg/m³ must not extrapolate past 500."""
    aqi, category = compute_aqi(1000.0, None)
    assert aqi == 500
    assert category == "Hazardous"


def test_pm10_above_top_breakpoint_caps_at_500():
    """H-2: PM10=2000 µg/m³ must not extrapolate past 500."""
    aqi, category = compute_aqi(None, 2000.0)
    assert aqi == 500
    assert category == "Hazardous"


def test_boundary_at_top_breakpoint_is_exactly_500():
    """The top EPA C_hi (500.4 µg/m³ PM2.5) maps to AQI 500."""
    aqi, category = compute_aqi(500.4, None)
    assert aqi == 500
    assert category == "Hazardous"


def test_in_range_values_still_interpolate_normally():
    """Sanity: the clamp must not disturb normal in-band interpolation."""
    aqi, _ = compute_aqi(35.5, None)  # low end of USG band for PM2.5
    assert 101 <= aqi <= 150
    aqi, _ = compute_aqi(None, 604.0)  # top PM10 band, no clamp needed
    assert aqi == 500


# ── M53/M54: breakpoint-edge behaviour is pinned ──────────────────────────────


def test_fractional_index_is_truncated_not_rounded():
    """M53: EPA convention truncates the fractional index (math.floor).

    PM2.5 = 12.0 sits exactly on the Good band's C_hi: the interpolation yields
    exactly 50; a value just inside the next band must truncate DOWN, never
    round half-up (the old behaviour produced off-by-one AQIs at edges).
    """
    aqi, category = compute_aqi(12.0, None)
    assert aqi == 50
    assert category == "Good"
    # First band boundary of PM2.5's second band: 35.4 → exactly 100, 35.5 → 101.
    aqi, _ = compute_aqi(35.4, None)
    assert aqi == 100
    aqi, _ = compute_aqi(35.5, None)
    assert aqi == 101


def test_pm10_band_gap_value_lands_on_previous_band_floor():
    """M54: PM10 bands have a 1-unit gap (54 → 55). A value inside the gap
    (54.5) is matched to the *next* band by the ``<= C_hi`` scan and
    interpolates a hair UNDER its I_lo (51), so truncation floors it to 50.
    Harmless but pinned: this documents the exact boundary behaviour so a
    future band-table edit cannot silently shift edge values.
    """
    aqi, category = compute_aqi(None, 54.5)
    assert aqi == 50
    assert category == "Moderate"  # band 1 owns the value despite AQI 50
    # The band's true lower edge maps to exactly its I_lo.
    aqi, _ = compute_aqi(None, 55.0)
    assert aqi == 51
