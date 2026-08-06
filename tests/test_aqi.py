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
