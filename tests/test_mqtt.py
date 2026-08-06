"""
MQTT layer tests — validator bounds (L-16/L-17/L-18/L-19), client dispatch
round-trip (would have caught C-1 and H-3), and config interval bounds (L-22).

These are pure-unit tests: no broker, no Redis, no Celery worker is required.
``mqtt.client`` imports the SQLAlchemy models at module load but never
connects, and dispatch is exercised through monkeypatched task/publish hooks.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import mqtt.client as mqtt_client
from mqtt.client import _dispatch_reading, _handle_reading, _truncated_repr
from mqtt.config import publish_config
from mqtt.validator import ReadingPayload, validate_reading, validate_status


# ── Validator bounds ───────────────────────────────────────────────────────────


def test_reading_rejects_infinity_and_nan():
    """L-16: voc_ohm/mq135_ppm and all float fields reject +Inf/NaN."""
    assert validate_reading({"voc_ohm": float("inf")}) is None
    assert validate_reading({"mq135_ppm": float("inf")}) is None
    assert validate_reading({"voc_ohm": float("-inf")}) is None
    assert validate_reading({"pm25": float("nan")}) is None
    assert validate_reading({"temperature": float("inf")}) is None
    assert validate_reading({"humidity": float("nan")}) is None
    assert validate_reading({"battery_v": float("inf")}) is None


def test_reading_rejects_bool_as_float():
    """L-17: a device sending bools must not silently become 1.0/0.0."""
    assert validate_reading({"temperature": True}) is None
    assert validate_reading({"pm25": False}) is None
    assert validate_reading({"humidity": True}) is None
    assert validate_reading({"battery_v": True}) is None


def test_reading_node_id_pattern():
    """L-18: body node_id must match ^[A-Za-z0-9_-]{1,50}$; absent is allowed."""
    assert validate_reading({"node_id": "ESP32-01", "pm25": 10.0}) is not None
    assert validate_reading({"node_id": "a" * 50, "pm25": 10.0}) is not None
    assert validate_reading({"node_id": "a/b", "pm25": 10.0}) is None
    assert validate_reading({"node_id": "x#y", "pm25": 10.0}) is None
    assert validate_reading({"node_id": "x+y", "pm25": 10.0}) is None
    assert validate_reading({"node_id": "a" * 51, "pm25": 10.0}) is None
    # Topic-only device (no body node_id) is not dropped.
    assert validate_reading({"pm25": 10.0}) is not None
    assert validate_reading({}) is not None


def test_reading_pressure_range_accepts_high_altitude():
    """L-19: pressure bounds loosened to 300–1250 so ~795 hPa is accepted."""
    assert validate_reading({"pressure": 795.0}) is not None
    assert validate_reading({"pressure": 300.0}) is not None
    assert validate_reading({"pressure": 1250.0}) is not None
    assert validate_reading({"pressure": 1013.0}) is not None
    assert validate_reading({"pressure": 299.0}) is None
    assert validate_reading({"pressure": 1251.0}) is None


def test_reading_still_rejects_out_of_range_sensor_values():
    """Existing range guards still hold after the L-16/L-19 changes."""
    assert validate_reading({"temperature": 61.0}) is None
    assert validate_reading({"humidity": -1.0}) is None
    assert validate_reading({"pm25": 2001.0}) is None
    assert validate_reading({"battery_v": 6.0}) is None


# ── Client dispatch round-trip (would have caught C-1 + H-3) ───────────────────


def test_dispatch_serializes_time_as_json_string(monkeypatch):
    """C-1: dispatch must send ``time`` as a JSON string, not a datetime.

    Kombu's JSON codec round-trips a datetime into a datetime on the worker,
    where ``_parse_time`` then crashes on ``datetime.replace(...)``.
    """
    captured = {}

    def fake_task():
        def delay(payload):
            captured["payload"] = payload
            return None

        return SimpleNamespace(delay=delay)

    monkeypatch.setattr(mqtt_client, "_get_process_reading_task", fake_task)
    payload = ReadingPayload(
        node_id="ESP32-01",
        time=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
        pm25=12.5,
    )
    _dispatch_reading("ESP32-01", payload)

    assert isinstance(captured["payload"]["time"], str)
    assert captured["payload"]["node_id"] == "ESP32-01"
    assert captured["payload"]["pm25"] == 12.5


def test_handle_reading_topic_node_id_is_authoritative(monkeypatch):
    """H-3: a spoofed body node_id cannot override the topic node_id."""
    captured = {}

    def fake_dispatch(node_id, payload):
        captured["node_id"] = node_id
        captured["payload_node_id"] = payload.node_id

    monkeypatch.setattr(mqtt_client, "_dispatch_reading", fake_dispatch)

    raw = json.dumps({"node_id": "EVIL-99", "pm25": 250.0})
    _handle_reading("ESP32-01", raw)

    assert captured["node_id"] == "ESP32-01"
    assert captured["payload_node_id"] == "ESP32-01"


def test_handle_reading_topic_only_device_is_dropped_only_on_invalid_body(monkeypatch):
    """H-3: a compliant topic-only device (no body node_id) still dispatches."""
    captured = {}

    def fake_dispatch(node_id, payload):
        captured["node_id"] = node_id
        captured["payload_node_id"] = payload.node_id

    monkeypatch.setattr(mqtt_client, "_dispatch_reading", fake_dispatch)

    _handle_reading("ESP32-01", json.dumps({"pm25": 12.0}))
    assert captured == {"node_id": "ESP32-01", "payload_node_id": "ESP32-01"}


def test_handle_reading_invalid_body_is_dropped(monkeypatch):
    """Invalid payloads still drop without reaching dispatch."""
    calls = []

    def fake_dispatch(node_id, payload):
        calls.append((node_id, payload))

    monkeypatch.setattr(mqtt_client, "_dispatch_reading", fake_dispatch)

    _handle_reading("ESP32-01", "{not json")
    _handle_reading("ESP32-01", json.dumps({"temperature": True}))  # bool rejected
    assert calls == []


def test_dispatch_retries_and_recovers(monkeypatch):
    """M-8: a transient delay() failure is retried, not log-and-dropped."""
    attempts = {"n": 0}

    class FlakyTask:
        def delay(self, payload):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("redis down")

    monkeypatch.setattr(mqtt_client, "_get_process_reading_task", lambda: FlakyTask())
    monkeypatch.setattr(mqtt_client.time, "sleep", lambda *_: None)

    payload = ReadingPayload(node_id="ESP32-01", pm25=10.0)
    _dispatch_reading("ESP32-01", payload)  # must not raise
    assert attempts["n"] == 3


def test_dispatch_gives_up_after_max_attempts(monkeypatch):
    """M-8: after the bounded retries the message is dropped with a log."""
    attempts = {"n": 0}

    class AlwaysFails:
        def delay(self, payload):
            attempts["n"] += 1
            raise RuntimeError("broker down")

    monkeypatch.setattr(mqtt_client, "_get_process_reading_task", lambda: AlwaysFails())
    monkeypatch.setattr(mqtt_client.time, "sleep", lambda *_: None)

    payload = ReadingPayload(node_id="ESP32-01", pm25=10.0)
    _dispatch_reading("ESP32-01", payload)
    assert attempts["n"] == mqtt_client._DISPATCH_MAX_ATTEMPTS


def test_payload_logging_is_truncated():
    """L-21: raw payloads must never be logged in full at WARNING."""
    long_payload = "x" * 5000
    truncated = _truncated_repr(long_payload)
    assert len(truncated) <= mqtt_client._LOG_TRUNCATE + 40
    assert truncated.startswith("'xxx")


# ── Config publisher bounds (L-22) ─────────────────────────────────────────────


class _FakePublish:
    def __init__(self) -> None:
        self.calls = []

    def publish(self, topic, payload, qos):
        self.calls.append((topic, payload, qos))
        return SimpleNamespace(rc=0)


@pytest.mark.parametrize("interval_s", [0, -5, 86401, 100000])
def test_publish_config_rejects_out_of_range_interval(interval_s):
    with pytest.raises(ValueError):
        publish_config(_FakePublish(), "ESP32-01", interval_s=interval_s)


def test_publish_config_accepts_boundary_intervals():
    client = _FakePublish()
    publish_config(client, "ESP32-01", interval_s=1)
    publish_config(client, "ESP32-01", interval_s=86400)
    assert len(client.calls) == 2


def test_publish_config_still_guards_node_id():
    with pytest.raises(ValueError):
        publish_config(_FakePublish(), "bad/id", interval_s=30)
    with pytest.raises(ValueError):
        publish_config(_FakePublish(), "bad#id", interval_s=30)


# ── Status payload sanity ──────────────────────────────────────────────────────


def test_status_payload_validates():
    assert validate_status({"online": True, "battery_v": 3.9}) is not None
    assert validate_status({"online": False}) is not None
    assert validate_status({"online": True, "battery_v": 6.0}) is None


# ── H-4: TLS fail-closed when certs are missing ────────────────────────────────


class _TLSCfg:
    """Minimal config stand-in with TLS enabled. Cert settings default to empty."""

    MQTT_USE_TLS = True
    MQTT_TLS_CERT = ""
    MQTT_TLS_KEY = ""
    MQTT_CA_CERTS = ""
    MQTT_BROKER_HOST = "localhost"
    MQTT_BROKER_PORT = 8883


def test_tls_fails_closed_when_cert_settings_empty(monkeypatch):
    """H-4: MQTT_USE_TLS=True with unset cert settings must abort construction."""
    monkeypatch.setattr(mqtt_client, "get_config", lambda: _TLSCfg())
    with pytest.raises(RuntimeError):
        mqtt_client.MQTTClient()


def test_tls_fails_closed_when_cert_file_missing(monkeypatch, tmp_path):
    """H-4: a set-but-unreadable cert file must also abort construction."""
    cfg = _TLSCfg()
    cfg.MQTT_TLS_CERT = str(tmp_path / "client.crt")  # does not exist on disk
    cfg.MQTT_TLS_KEY = str(tmp_path / "client.key")
    cfg.MQTT_CA_CERTS = str(tmp_path / "ca.crt")
    monkeypatch.setattr(mqtt_client, "get_config", lambda: cfg)
    with pytest.raises(RuntimeError):
        mqtt_client.MQTTClient()


def test_start_refuses_when_tls_requested_but_not_configured(monkeypatch):
    """H-4: start() must raise (not plaintext/reconnect-loop) if TLS is unset."""
    cfg = _TLSCfg()
    monkeypatch.setattr(mqtt_client, "get_config", lambda: cfg)
    client = mqtt_client.MQTTClient.__new__(mqtt_client.MQTTClient)  # skip __init__
    client._cfg = cfg
    client._tls_configured = False
    with pytest.raises(RuntimeError):
        client.start()


def test_mqtt_client_registry():
    """set_client/get_client round-trip; None clears."""
    import mqtt.registry as reg
    dummy = object()
    reg.set_client(dummy)
    assert reg.get_client() is dummy
    reg.set_client(None)
    assert reg.get_client() is None
