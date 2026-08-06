"""Unit tests for the Tsukamoto fuzzy inference engine.

Pure tests — no DB, no Redis, no pytest fixtures.  They can be run with
``pytest tests/test_fuzzy.py -q`` or by any plain test runner.
"""

from collections import Counter

import pytest

from fuzzy.membership import (
    HUM_TERMS,
    PM_TERMS,
    TEMP_TERMS,
    hum_dry,
    hum_humid,
    hum_wet,
    pm_high,
    pm_low,
    pm_medium,
    temp_high,
    temp_low,
    temp_medium,
    tri,
)
from fuzzy.rules import (
    CONSEQUENTS,
    GOOD,
    MODERATE,
    RULE_TABLE,
    UNHEALTHY,
    USG,
    VERY_UNHEALTHY,
)
from fuzzy.tsukamoto import DEFAULT_SCORE, _weighted_average, fuzzy_score, infer


def _almost(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


# ── Membership functions: peaks/shoulders == 1, support edges == 0 ────────────

def test_temperature_membership_shoulders_and_edges():
    # Low has a shoulder on [0, 15] so a sub-zero / offset reading fires a rule.
    assert temp_low(0) == 1.0
    assert temp_low(10) == 1.0
    assert temp_low(15) == 1.0
    assert temp_low(25) == 0.5  # crosses medium at 0.5
    assert temp_low(35) == 0.0
    assert temp_medium(15) == 0.0
    assert temp_medium(25) == 0.5
    assert temp_medium(35) == 1.0
    assert temp_medium(40) == 1.0
    assert temp_medium(45) == 0.5
    assert temp_medium(50) == 0.0
    assert temp_high(40) == 0.0
    assert temp_high(45) == 0.5
    assert temp_high(50) == 1.0


def test_humidity_membership_shoulders_and_edges():
    assert hum_dry(0) == 1.0
    assert hum_dry(30) == 1.0
    assert hum_dry(50) == 0.5  # crosses humid at 0.5
    assert hum_dry(70) == 0.0
    assert hum_humid(30) == 0.0
    assert hum_humid(50) == 0.5
    assert hum_humid(70) == 1.0
    assert hum_humid(80) == 1.0
    assert hum_humid(90) == 0.5
    assert hum_humid(100) == 0.0
    assert hum_wet(80) == 0.0
    assert hum_wet(90) == 0.5
    assert hum_wet(100) == 1.0


def test_pm_membership_shoulders_and_edges():
    assert pm_low(0) == 1.0
    assert pm_low(50) == 1.0
    assert pm_low(75) == 0.5  # crosses medium at 0.5
    assert pm_low(100) == 0.0
    assert pm_medium(50) == 0.0
    assert pm_medium(75) == 0.5
    assert pm_medium(100) == 1.0
    assert pm_medium(200) == 1.0
    assert pm_medium(250) == 0.5
    assert pm_medium(300) == 0.0
    assert pm_high(200) == 0.0
    assert pm_high(250) == 0.5
    assert pm_high(300) == 1.0
    assert pm_high(500) == 1.0


def test_memberships_clamp_out_of_domain_inputs():
    # A sub-zero reading behaves like the low shoulder (fires a rule, H-1).
    assert temp_low(-10) == temp_low(0) == 1.0
    assert temp_high(60) == 1.0  # clamped to the 50 °C domain top
    assert hum_wet(-5) == 0.0
    assert hum_wet(200) == 1.0
    assert pm_high(1000) == 1.0
    assert pm_low(600) == 0.0


# ── Partition completeness (M-1): memberships sum to 1, adjacent cross at 0.5 ─

def test_temperature_partition_sums_to_one():
    for i in range(0, 501):
        t = i / 10.0
        assert _almost(temp_low(t) + temp_medium(t) + temp_high(t), 1.0, tol=1e-9)


def test_humidity_partition_sums_to_one():
    for i in range(0, 1001):
        h = i / 10.0
        assert _almost(hum_dry(h) + hum_humid(h) + hum_wet(h), 1.0, tol=1e-9)


def test_pm_partition_sums_to_one():
    for i in range(0, 5001):
        p = i / 10.0
        assert _almost(pm_low(p) + pm_medium(p) + pm_high(p), 1.0, tol=1e-9)


# ── tri() guard (L-1): degenerate triangles never divide by zero ──────────────

def test_tri_degenerate_never_divides_by_zero():
    # Left shoulder (a == b): plateau flush with the edge, no ZeroDivisionError.
    assert tri(5, 0, 0, 10) == 1.0
    # Peak at the right edge (b == c): rising ramp, no ZeroDivisionError.
    assert _almost(tri(5, 0, 10, 10), 0.5)
    # Fully degenerate: returns 0 (outside support).
    assert tri(5, 0, 0, 0) == 0.0


# ── Rule base: all 27 combinations present, deterministic ─────────────────────

def test_rule_table_covers_exactly_all_27_combinations():
    expected = {(t, h, p) for t in TEMP_TERMS for h in HUM_TERMS for p in PM_TERMS}
    assert set(RULE_TABLE) == expected
    assert len(RULE_TABLE) == 27


def test_rule_table_consequent_totals():
    counts = Counter(RULE_TABLE.values())
    assert counts == {
        GOOD: 4,
        MODERATE: 9,
        USG: 5,
        UNHEALTHY: 8,
        VERY_UNHEALTHY: 1,
    }
    assert sum(counts.values()) == 27


def test_every_consequent_label_has_monotonic_ramp():
    assert set(RULE_TABLE.values()) <= set(CONSEQUENTS)
    for lo, hi in CONSEQUENTS.values():
        assert 0.0 <= lo < hi <= 100.0


# ── Inference: crisp scores ───────────────────────────────────────────────────

def test_infer_returns_expected_keys():
    result = infer(20, 50, 10)
    assert set(result) == {"score", "rules_fired"}
    assert isinstance(result["score"], float)
    assert isinstance(result["rules_fired"], int)


def test_clean_air_scores_low():
    result = infer(20, 50, 10)
    assert result["rules_fired"] >= 1
    assert 0.0 <= result["score"] <= 100.0
    assert result["score"] < 30.0


def test_heavy_pollution_scores_high():
    result = infer(20, 50, 250)
    assert result["rules_fired"] >= 1
    assert result["score"] > 60.0


def test_very_heavy_pollution_saturates_at_100():
    assert _almost(infer(99, 0, 9999)["score"], 100.0)


def test_scores_stay_within_0_100_grid():
    for t in (0, 25, 50):
        for h in (0, 50, 100):
            for p in (0, 50, 250, 500):
                assert 0.0 <= fuzzy_score(t, h, p) <= 100.0


def test_infer_is_deterministic():
    assert infer(30, 70, 120) == infer(30, 70, 120)
    assert fuzzy_score(30, 70, 120) == infer(30, 70, 120)["score"]


# ── Monotonicity (M-1 / L-2): PM2.5 sweep across many (T, H) contexts ─────────

def test_pollution_monotonic_first_context():
    scores = [infer(20, 50, p)["score"] for p in (10, 50, 100, 200, 300, 500)]
    assert scores == sorted(scores)


def test_pollution_monotonic_second_context():
    scores = [infer(25, 60, p)["score"] for p in (10, 50, 100, 250)]
    assert scores == sorted(scores)


def test_pollution_monotonic_full_context_sweep():
    # Monotonicity on the *sampled* p-grid: raising PM2.5 across the discrete
    # p-values (10/50/100/200/300/500 µg/m³) never lowers the score, at any of
    # the sampled (t, h) contexts. NOTE: this asserts monotonicity only at those
    # six p samples. It does NOT, and cannot, prove *continuous* monotonicity in
    # PM2.5 — that is a known structural residual of the Tsukamoto weighted
    # average (docs/known-issues.md N-10): between the sampled points the score
    # can dip by ~9 pts when a higher-consequent rule begins firing at a term
    # boundary (its crisp z sits near its ramp floor, below the continuing
    # lower-consequent rule's z). Treated as a product-accepted limitation; see
    # the N-10 registry entry for the decision and the fix options.
    for t in (0, 10, 20, 30, 40, 50):
        for h in (0, 25, 50, 75, 100):
            scores = [infer(t, h, p)["score"] for p in (10, 50, 100, 200, 300, 500)]
            assert scores == sorted(scores), f"non-monotonic at T={t}, H={h}"


# ── Input validation (M-2): None / NaN / Inf rejected at the boundary ─────────

def test_infer_rejects_none_inputs():
    with pytest.raises(ValueError):
        infer(None, 20, 250)
    with pytest.raises(ValueError):
        infer(20, None, 250)
    with pytest.raises(ValueError):
        infer(20, 50, None)


def test_infer_rejects_non_finite_inputs():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            infer(bad, 20, 250)
        with pytest.raises(ValueError):
            infer(20, 50, bad)


# ── No-rule-fires fallback (H-1 / L-2) ────────────────────────────────────────

def test_no_fired_rules_returns_default_good_score():
    # The fallback is exercised directly through the aggregation helper.
    assert _weighted_average([]) == DEFAULT_SCORE == 20.0


def test_in_domain_readings_always_fire_a_rule():
    # The term partitions cover each domain exactly, so every in-domain reading
    # fires at least one rule (the no-fire fallback is unreachable in practice).
    for t in (0, 10, 25, 40, 50):
        for h in (0, 30, 60, 90, 100):
            for p in (0, 60, 250, 500):
                assert infer(t, h, p)["rules_fired"] >= 1