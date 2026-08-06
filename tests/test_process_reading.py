"""Regression tests for the per-reading enrichment task (C-1).

C-1: Kombu's JSON codec reconstructs ``datetime`` objects on the worker, and
``_parse_time`` only handled ``str`` — it called ``value.replace("Z", ...)`` on
the ``datetime`` and raised ``TypeError`` (only ``ValueError`` was caught), so
every timestamped reading was silently dropped.  These tests pin the
``datetime | str | None`` contract.
"""

from datetime import datetime, timedelta, timezone

from models import SensorReading
from tasks.forecast import (
    _get_model,
    _valid_model,
    generate_forecast,
    retrain_model,
)
from tasks.process_reading import _build_reading, _parse_time, detect_anomaly

from tasks import process_reading


def test_parse_time_accepts_datetime():
    dt = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    assert _parse_time(dt) == dt


def test_parse_time_accepts_naive_datetime():
    dt = datetime(2026, 8, 5, 12, 0)  # no tzinfo
    result = _parse_time(dt)
    assert result.tzinfo is not None
    # Contract: naive timestamps are treated as UTC (not interpreted in the
    # process-local timezone), so the result equals the naive value tagged UTC.
    assert result == dt.replace(tzinfo=timezone.utc)


def test_parse_time_accepts_iso_string_with_z():
    result = _parse_time("2026-08-05T12:00:00Z")
    assert result == datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


# ── H-1: fuzzy score must not collapse to 20 when temperature is missing/0 ─────

def test_missing_temperature_substitutes_neutral_value_and_fires_rules():
    """H-1b: no temperature in the payload -> 25.0 °C (domain midpoint), so a
    heavy-pollution reading fires fuzzy rules instead of returning the 20.0
    'Good' fallback (which would contradict the row's ~200 AQI)."""
    reading = _build_reading({"pm25": 250.0}, "ESP32-01")
    assert reading.temperature is None  # stored as missing, not a fake 0.0
    assert reading.fuzzy_score > 20.0


def test_zero_and_subzero_temperature_still_fire_rules():
    """H-1a: the temperature 'Low' shoulder covers 0 °C / sub-zero, so even an
    explicit 0/-5 °C reading fires rules and never falls back to 20.0."""
    for t in (0.0, -5.0):
        reading = _build_reading({"pm25": 250.0, "temperature": t}, "ESP32-01")
        assert reading.fuzzy_score > 20.0


# ── M-2: NaN / Inf / None sensor values are treated as MISSING ────────────────

def test_nan_temperature_is_missing_and_neutral_substitution_fires_rules():
    """M-2: a NaN temperature must not clamp to a domain top (worst-case score);
    it is stored as missing and the 25.0 °C neutral substitution applies."""
    reading = _build_reading({"pm25": 250.0, "temperature": float("nan")}, "ESP32-01")
    assert reading.temperature is None
    assert reading.fuzzy_score > 20.0  # fires rules via the neutral 25.0 °C


def test_nan_sensor_values_are_stored_as_missing():
    """M-2: NaN/Inf pm25, humidity, pressure, pm10 all map to NULL (missing)."""
    reading = _build_reading(
        {
            "temperature": 25.0,
            "humidity": float("nan"),
            "pm25": float("inf"),
            "pm10": float("-inf"),
            "pressure": float("nan"),
        },
        "ESP32-01",
    )
    assert reading.humidity is None
    assert reading.pm25 is None
    assert reading.pm10 is None
    assert reading.pressure is None
    assert reading.temperature == 25.0  # finite values pass through untouched
    # Both pollutants missing -> AQI is skipped entirely.
    assert reading.aqi is None and reading.aqi_category is None


def test_none_sensor_values_are_stored_as_missing():
    """M-2: explicit None payload fields map to NULL, never to a domain edge."""
    reading = _build_reading(
        {"temperature": None, "humidity": None, "pm25": None}, "ESP32-01"
    )
    assert reading.temperature is None
    assert reading.humidity is None
    assert reading.pm25 is None


# ── L-6: anomaly window must load ~24h of samples (not ~8.3h) ─────────────────

def test_anomaly_window_limit_matches_24h_at_30s_interval():
    """L-6: 2880 samples = 24h at the standard 30s cadence, matching the
    documented window (the old hardcoded 1000 capped it at ~8.3h)."""
    assert process_reading._ANOMALY_WINDOW_SAMPLES == 2880


class _FakeRedis:
    """Tiny Redis stub for hermetic (no-live-Redis) tests."""

    def __init__(self, stored: dict | None = None):
        self.stored = stored or {}
        self.setex_calls: list[tuple] = []
        self.delete_calls: list[str] = []

    def get(self, key):
        return self.stored.get(key)

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))

    def delete(self, key):
        self.delete_calls.append(key)


# ── L-10: corrupted model blob must never 500 the forecast route ──────────────

def test_valid_model_accepts_finite_numeric_slope_and_intercept():
    assert _valid_model({"slope": 0.5, "intercept": 10, "trained_at": "x"}) is True


def test_valid_model_rejects_string_slope_or_intercept():
    """L-10: json.loads parses '{"slope": "0.5"}' fine but the value type is
    wrong — previously reached ``slope * ts.timestamp()`` and raised TypeError."""
    assert _valid_model({"slope": "0.5", "intercept": 10}) is False
    assert _valid_model({"slope": 0.5, "intercept": "10"}) is False


def test_valid_model_rejects_bool_nonfinite_and_missing_values():
    assert _valid_model({"slope": True, "intercept": 10}) is False
    assert _valid_model({"slope": float("nan"), "intercept": 10}) is False
    assert _valid_model({"slope": float("inf"), "intercept": 10}) is False
    assert _valid_model({"slope": 0.5}) is False  # intercept missing
    assert _valid_model(None) is False
    assert _valid_model("not a dict") is False


def test_get_model_rejects_corrupted_blob_and_returns_none(monkeypatch):
    client = _FakeRedis(
        {"forecast:model:N1": '{"slope": "oops", "intercept": 5}'}
    )
    monkeypatch.setattr("tasks.forecast._redis", lambda: client)
    assert _get_model("N1") is None


def test_generate_forecast_falls_back_to_training_on_corrupted_blob(monkeypatch):
    """L-10: a string-slope cached blob makes _get_model return None, so
    generate_forecast must train on the fly instead of raising TypeError."""
    client = _FakeRedis(
        {"forecast:model:N1": '{"slope": "oops", "intercept": 5}'}
    )
    monkeypatch.setattr("tasks.forecast._redis", lambda: client)
    monkeypatch.setattr(
        "tasks.forecast._fit_model",
        lambda points: {"slope": 0.1, "intercept": 5, "trained_at": "t"},
    )
    monkeypatch.setattr(
        "tasks.forecast._training_points", lambda node_id: [(1000.0, 5.0)] * 30
    )
    points = generate_forecast("N1")
    assert len(points) == 60
    assert all("time" in p and isinstance(p["aqi"], float) for p in points)


# ── L-11: retrain_model must invalidate the served forecast cache key ─────────

def test_retrain_model_deletes_served_forecast_key(monkeypatch):
    """L-11: after (re)writing a model, retrain_model must also drop the
    served ``celery:forecast:{node_id}`` key so it is not stale up to 1h."""
    client = _FakeRedis()

    class _FakeSession:
        def scalars(self, stmt):
            return self

        def all(self):
            return ["N1"]

    class _Ctx:
        def __enter__(self):
            return _FakeSession()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("tasks.forecast.get_sync_db", _Ctx)
    monkeypatch.setattr(
        "tasks.forecast._training_points", lambda node_id: [(1000.0, 5.0)] * 30
    )
    monkeypatch.setattr(
        "tasks.forecast._fit_model",
        lambda points: {"slope": 0.1, "intercept": 5, "trained_at": "t"},
    )
    monkeypatch.setattr("tasks.forecast._redis", lambda: client)

    assert retrain_model() == {"models": 1}
    assert any(k.startswith("forecast:model:N1") for k, *_ in client.setex_calls)
    assert "celery:forecast:N1" in client.delete_calls


# ── L-28 (task half): write-through must invalidate the global key ────────────

def test_write_latest_cache_invalidates_global_key(monkeypatch):
    """L-28: the task-side write-through must delete the served global
    ``readings:latest`` key so the API snapshot is never stale up to 60s."""
    client = _FakeRedis()
    monkeypatch.setattr(
        "tasks.process_reading._get_redis_client", lambda: client
    )
    process_reading._write_latest_cache("N1", {"node_id": "N1"})
    assert any(k.startswith("readings:latest:N1") for k, *_ in client.setex_calls)
    assert process_reading._LATEST_GLOBAL_KEY in client.delete_calls


# ── detect_anomaly: SQL-aggregate rewrite keeps the z-score contract ──────────

def _seed_pm25_history(db_session, node_id: str, values: list[float], *, step=timedelta(minutes=1)) -> None:
    """Insert one ``SensorReading`` per value, back from now at ``step`` spacing."""
    now = datetime.now(timezone.utc)
    for i, v in enumerate(values):
        db_session.add(SensorReading(time=now - step * i, node_id=node_id, pm25=v))
    db_session.flush()


def test_detect_anomaly_false_when_no_pm25(db_session):
    assert detect_anomaly(db_session, "ANOM-01", None) is False


def test_detect_anomaly_false_below_min_samples(db_session, sample_node):
    n = process_reading._ANOMALY_MIN_SAMPLES - 1
    _seed_pm25_history(db_session, sample_node.node_id, [20.0] * n)
    assert detect_anomaly(db_session, sample_node.node_id, 20.0) is False


def test_detect_anomaly_false_on_zero_variance(db_session, sample_node):
    _seed_pm25_history(db_session, sample_node.node_id, [20.0] * 10)
    # Invariant baseline → no Z-score is meaningful, even for a large outlier.
    assert detect_anomaly(db_session, sample_node.node_id, 50.0) is False


def test_detect_anomaly_true_on_clear_outlier(db_session, sample_node):
    # Baseline alternates 20/21 → mean 20.5, std 0.5; z(50.0) ≈ 59 > 3.
    _seed_pm25_history(db_session, sample_node.node_id, [20.0, 21.0] * 5)
    assert detect_anomaly(db_session, sample_node.node_id, 50.0) is True


def test_detect_anomaly_keeps_2880_most_recent_samples(db_session, sample_node):
    """The aggregate must keep the LIMIT: the 2880 most-recent samples define
    the baseline, so an older outlier cannot widen the variance and mask a
    breach. 2881 readings at 10s spacing (≈8h) all sit inside the 24h window,
    so only the LIMIT can drop the oldest."""
    nid = sample_node.node_id
    _seed_pm25_history(
        db_session, nid, [20.0, 21.0] * 1440 + [100.0], step=timedelta(seconds=10)
    )
    # Most-recent 2880 → mean 20.5, std 0.5 → z(22.2) = 3.4 → True. If the 100.0
    # leaked past the LIMIT, std ≈ 1.56 → z(22.2) ≈ 1.1 → False.
    assert detect_anomaly(db_session, nid, 22.2) is True
