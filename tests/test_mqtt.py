"""
MQTT layer tests — validator bounds (L-16/L-17/L-18/L-19), client dispatch
round-trip (would have caught C-1 and H-3), and config interval bounds (L-22).

These are pure-unit tests: no broker, no Redis, no Celery worker is required.
``mqtt.client`` imports the SQLAlchemy models at module load but never
connects, and dispatch is exercised through monkeypatched task/publish hooks.
"""

from __future__ import annotations

import json
import queue
from collections import deque
from datetime import datetime, timedelta, timezone
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


# ── L70: inbound payload size cap + firmware bound ─────────────────────────────


def _bare_mqtt_client() -> mqtt_client.MQTTClient:
    """An MQTTClient without __init__ (no broker/config), queue wired up."""
    client = mqtt_client.MQTTClient.__new__(mqtt_client.MQTTClient)
    client._queue = queue.Queue(maxsize=100)
    return client


def test_on_message_drops_oversized_payload_before_decode():
    """L70: payloads over _MAX_PAYLOAD_BYTES never reach decode/enqueue."""
    client = _bare_mqtt_client()
    payload = b"{" + b"x" * (mqtt_client._MAX_PAYLOAD_BYTES + 10) + b"}"
    msg = SimpleNamespace(topic="air/node/ESP32-BIG/reading", payload=payload)
    client._on_message(None, None, msg)
    assert client._queue.qsize() == 0


def test_on_message_accepts_payload_at_the_cap():
    """L70: a payload exactly at the cap is still accepted."""
    client = _bare_mqtt_client()
    body = {"pm25": 10.0, "pad": ""}
    base_len = len(json.dumps(body).encode())
    body["pad"] = "x" * (mqtt_client._MAX_PAYLOAD_BYTES - base_len)
    payload = json.dumps(body).encode()
    assert len(payload) == mqtt_client._MAX_PAYLOAD_BYTES
    msg = SimpleNamespace(topic="air/node/ESP32-CAP/reading", payload=payload)
    client._on_message(None, None, msg)
    assert client._queue.qsize() == 1


def test_status_firmware_length_is_bounded():
    """L70: firmware is capped so it can't bloat validated payloads."""
    assert validate_status({"online": True, "firmware": "v" * 64}) is not None
    assert validate_status({"online": True, "firmware": "v" * 65}) is None


# ── M101: backlog replay bypasses the per-node rate limiter ────────────────────


def test_device_time_is_stale_classification():
    """M101: only an explicitly old device timestamp counts as backlog."""
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    assert mqtt_client._device_time_is_stale(json.dumps({"time": old})) is True
    assert mqtt_client._device_time_is_stale(json.dumps({"time": fresh})) is False
    assert mqtt_client._device_time_is_stale(json.dumps({"pm25": 1.0})) is False
    assert mqtt_client._device_time_is_stale("{not json") is False


def test_on_message_backlog_burst_bypasses_rate_limiter(monkeypatch):
    """M101: replayed backlog (old device time) survives the reconnect burst."""
    monkeypatch.setattr(mqtt_client, "_node_last_seen", {})
    client = _bare_mqtt_client()
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    for i in range(5):
        payload = json.dumps({"pm25": float(i), "time": old}).encode()
        msg = SimpleNamespace(topic="air/node/ESP32-BL/reading", payload=payload)
        client._on_message(None, None, msg)
    assert client._queue.qsize() == 5


def test_on_message_fresh_burst_still_rate_limited(monkeypatch):
    """M101: fresh (current-timestamp) messages stay rate-limited as before."""
    monkeypatch.setattr(mqtt_client, "_node_last_seen", {})
    client = _bare_mqtt_client()
    fresh = datetime.now(timezone.utc).isoformat()
    for i in range(5):
        payload = json.dumps({"pm25": float(i), "time": fresh}).encode()
        msg = SimpleNamespace(topic="air/node/ESP32-FL/reading", payload=payload)
        client._on_message(None, None, msg)
    assert client._queue.qsize() == 1


# ── M102: _node_last_seen is bounded ───────────────────────────────────────────


def test_node_last_seen_is_bounded_with_eviction(monkeypatch):
    """M102: unique node ids can't grow _node_last_seen without bound."""
    monkeypatch.setattr(mqtt_client, "_node_last_seen", {})
    for i in range(mqtt_client._NODE_LAST_SEEN_MAX + 1):
        mqtt_client._node_rate_limited(f"flood-{i}")
    seen = mqtt_client._node_last_seen
    assert len(seen) <= mqtt_client._NODE_LAST_SEEN_MAX
    assert f"flood-{mqtt_client._NODE_LAST_SEEN_MAX}" in seen  # newest survives
    assert "flood-0" not in seen  # oldest evicted first


# ── L69: failed subscribes are retried from the worker tick ────────────────────


def test_subscribe_failure_is_flagged_and_retried():
    """L69: a failed subscribe() in _on_connect is retried, not stuck forever."""
    client = mqtt_client.MQTTClient.__new__(mqtt_client.MQTTClient)
    client._pending_subs = {}
    client._granted_topics = set()
    client._ready = False
    client._subscribe_failed = False
    client._next_subscribe_retry = 0.0

    calls = []

    class FakePaho:
        fail = True

        def is_connected(self):
            return True

        def subscribe(self, topic, qos):
            calls.append(topic)
            if self.fail:
                return (mqtt_client.mqtt.MQTT_ERR_NO_CONN, 0)
            return (mqtt_client.mqtt.MQTT_ERR_SUCCESS, len(calls))

    fake = FakePaho()
    client._client = fake

    client._on_connect(fake, None, {}, 0)
    assert client._subscribe_failed is True
    assert client._pending_subs == {}

    client._maybe_retry_subscribes()  # still failing → nothing registered
    assert client._pending_subs == {}

    fake.fail = False
    client._next_subscribe_retry = 0.0  # bypass the throttle for the test
    client._maybe_retry_subscribes()
    assert len(client._pending_subs) == 3

    for mid, topic in list(client._pending_subs.items()):
        client._on_subscribe(fake, None, mid, (1,))
    assert client._subscribe_failed is False
    assert client._ready is True


def test_suback_denial_flags_subscribe_retry():
    """L69: a broker-denied subscription is also retried, not stuck forever."""
    client = mqtt_client.MQTTClient.__new__(mqtt_client.MQTTClient)
    client._pending_subs = {7: "air/node/+/reading"}
    client._granted_topics = set()
    client._ready = False
    client._subscribe_failed = False

    client._on_subscribe(None, None, 7, (0x80,))
    assert client._subscribe_failed is True
    assert client._ready is False


# ── M99/M100: alert publisher client id + retry budget ─────────────────────────


class _FakePublisherClient:
    def __init__(self, rc=0, raise_after=None):
        self.rc = rc
        self.raise_after = raise_after
        self.published = []

    def is_connected(self):
        return True

    def publish(self, topic, payload, qos):
        if self.raise_after is not None and len(self.published) >= self.raise_after:
            raise RuntimeError("broker went away mid-retry")
        self.published.append((topic, payload, qos))
        return SimpleNamespace(rc=self.rc)


def _alert_items(count):
    return [
        ({"node_id": f"N{i}", "aqi": 1.0, "category": None,
          "severity": "info", "timestamp": "2026-08-07T00:00:00Z"}, 0)
        for i in range(count)
    ]


def test_publisher_client_id_is_unique_per_host_and_pid(monkeypatch):
    """M99: no hardcoded client id — hostname/PID suffix as in H36."""
    import os
    import socket

    import mqtt.publisher as publisher

    captured = {}

    class FakePahoClient:
        def __init__(self, client_id=None, protocol=None, **kwargs):
            captured["client_id"] = client_id

        def tls_set(self, *args, **kwargs):
            pass

        def connect_async(self, *args, **kwargs):
            pass

        def loop_start(self):
            pass

    cfg = SimpleNamespace(
        MQTT_USE_TLS=False, MQTT_BROKER_HOST="localhost", MQTT_BROKER_PORT=1883
    )
    monkeypatch.setattr(publisher, "get_config", lambda: cfg)
    monkeypatch.setattr(publisher.mqtt, "Client", FakePahoClient)
    monkeypatch.setattr(publisher, "_client", None)

    publisher._get_client()

    client_id = captured["client_id"]
    assert client_id.startswith("empyrean-alert-publisher-")
    assert socket.gethostname().lower() in client_id
    assert str(os.getpid()) in client_id


def test_retry_budget_processes_at_most_batch_size(monkeypatch):
    """M100: per-call retry budget leaves the rest in the deque."""
    import mqtt.publisher as publisher

    failed = deque(_alert_items(25))
    monkeypatch.setattr(publisher, "_failed_queue", failed)
    fake = _FakePublisherClient(rc=0)
    monkeypatch.setattr(publisher, "_get_client", lambda: fake)

    publisher._retry_failed_messages()
    assert len(fake.published) == publisher._RETRY_BATCH_SIZE
    assert len(failed) == 25 - publisher._RETRY_BATCH_SIZE

    publisher._retry_failed_messages()
    assert len(fake.published) == 2 * publisher._RETRY_BATCH_SIZE
    assert len(failed) == 5


def test_retry_reenqueues_batch_when_interrupted(monkeypatch):
    """M100: a mid-retry exception must not lose the outstanding batch."""
    import mqtt.publisher as publisher

    failed = deque(_alert_items(10))
    monkeypatch.setattr(publisher, "_failed_queue", failed)
    fake = _FakePublisherClient(rc=0, raise_after=3)
    monkeypatch.setattr(publisher, "_get_client", lambda: fake)

    with pytest.raises(RuntimeError):
        publisher._retry_failed_messages()

    assert len(fake.published) == 3
    assert len(failed) == 7  # in-flight message + 6 unprocessed re-enqueued


def test_retry_reenqueues_failed_publish_with_incremented_attempt(monkeypatch):
    """M100: a non-zero publish rc goes back in the deque with attempt+1."""
    import mqtt.publisher as publisher

    failed = deque(_alert_items(1))
    monkeypatch.setattr(publisher, "_failed_queue", failed)
    fake = _FakePublisherClient(rc=1)
    monkeypatch.setattr(publisher, "_get_client", lambda: fake)

    publisher._retry_failed_messages()
    assert len(failed) == 1
    assert failed[0][1] == 1


def test_retry_drops_message_after_max_attempts(monkeypatch):
    """M100: messages that exhaust the retry budget are dropped, not retried."""
    import mqtt.publisher as publisher

    payload, _ = _alert_items(1)[0]
    failed = deque([(payload, publisher._MAX_RETRY_ATTEMPTS)])
    monkeypatch.setattr(publisher, "_failed_queue", failed)
    fake = _FakePublisherClient(rc=0)
    monkeypatch.setattr(publisher, "_get_client", lambda: fake)

    publisher._retry_failed_messages()
    assert len(failed) == 0
    assert fake.published == []


def test_publish_alert_still_never_raises_when_retry_interrupted(monkeypatch):
    """M100: a retry failure must not escape publish_alert's never-raises contract."""
    import mqtt.publisher as publisher

    monkeypatch.setattr(publisher, "_failed_queue", deque())
    monkeypatch.setattr(publisher, "_get_client", lambda: _FakePublisherClient(rc=0))

    def boom():
        raise RuntimeError("interrupted mid-retry")

    monkeypatch.setattr(publisher, "_retry_failed_messages", boom)
    publisher.publish_alert("N1", 150.0, "Unhealthy", "critical", "2026-08-07T00:00:00Z")
