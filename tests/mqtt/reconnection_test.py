"""
Integration tests for MQTT client reconnection behavior.

Tests the MQTTClient lifecycle under various network failure scenarios,
including broker disconnection, network timeout, and TLS handshake failures.

Run with: pytest tests/mqtt/reconnection_test.py -v
"""

import queue
import time
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest

from mqtt.client import (
    _CLIENT_ID,
    _ENQUEUE_MAX_ATTEMPTS,
    _TOPIC_RE,
    _handle_reading,
    MQTTClient,
)


@pytest.fixture
def mqtt_client():
    """Create a fresh MQTT client for each test."""
    client = MQTTClient()
    client._ready = False  # Skip ready check
    yield client
    client.stop()


class TestMQTTReconnection:
    """Test MQTT client reconnection and network failure handling."""

    @pytest.mark.integration
    def test_on_connect_resubscribes_on_reconnect(self, mqtt_client):
        """Test that client re-establishes subscriptions after reconnection."""
        subscribe_call_count = 0

        def mock_subscribe(topic, qos):
            nonlocal subscribe_call_count
            subscribe_call_count += 1
            # Simulate SUBACK with granted QoS
            return (0, subscribe_call_count)  # Success

        with patch.object(mqtt_client._client, 'subscribe', side_effect=mock_subscribe):
            # Simulate initial connect
            mqtt_client._on_connect(mqtt_client._client, None, None, 0)
            assert subscribe_call_count == 3  # 3 topics: reading, status, alerts

            # Simulate reconnection
            mqtt_client._on_connect(mqtt_client._client, None, None, 0)
            assert subscribe_call_count == 6  # Resubscribed

    @pytest.mark.integration
    def test_on_connect_logs_consecutive_errors(self, mqtt_client, caplog):
        """Test that broker connection failures are logged appropriately."""
        with caplog.at_level("ERROR"):
            # Simulate broker refusing connection
            mqtt_client._on_connect(mqtt_client._client, None, None, 5)  # Refused

            assert "Broker connection refused with rc=5" in caplog.text
            assert "will retry" in caplog.text

    @pytest.mark.integration
    def test_on_subscribe_with_granted_qos(self, mqtt_client):
        """M109: SUBACK grants are recorded, mids consumed, readiness flips.

        The old assertion (``topic in _pending_subs or not _pending_subs``)
        was tautological — it passed regardless of the outcome.
        """
        from mqtt.client import _READING_TOPIC, _STATUS_TOPIC

        # Simulate the two required SUBSCRIBEs and their SUBACKs.
        mqtt_client._pending_subs[1] = _READING_TOPIC
        mqtt_client._pending_subs[2] = _STATUS_TOPIC
        mqtt_client._on_subscribe(mqtt_client._client, None, 1, (1,))
        assert mqtt_client._ready is False  # only 1/2 granted so far

        mqtt_client._on_subscribe(mqtt_client._client, None, 2, (1,))

        assert _READING_TOPIC in mqtt_client._granted_topics
        assert _STATUS_TOPIC in mqtt_client._granted_topics
        assert mqtt_client._pending_subs == {}  # mids consumed by the SUBACKs
        assert mqtt_client._ready is True  # M36: readiness by granted topic set

    @pytest.mark.integration
    def test_on_subscribe_with_denied_qos(self, mqtt_client, caplog):
        """Test subscription denial is logged."""
        topic = "air/node/test_node/reading"

        with patch.object(mqtt_client._client, "subscribe", return_value=(0, 1)):
            result, mid = mqtt_client._client.subscribe(topic, 1)
            assert result == 0

        # Simulate broker denying (0x80 is failure in MQTT)
        with caplog.at_level("ERROR"):
            mqtt_client._pending_subs[mid] = topic
            mqtt_client._on_subscribe(mqtt_client._client, None, mid, (128,))

        assert "denied" in caplog.text

    @pytest.mark.integration
    def test_message_handling_on_reconnect(self, mqtt_client, caplog):
        """Test that messages still route correctly regardless of reconnection."""
        raw_payload = '{"pm25":12.5,"pm10":45.0,"node_id":"test_node","timestamp":"2026-08-20T12:00:00Z"}'

        with patch("mqtt.client._dispatch_reading") as mock_dispatch:
            # Process message on a known topic
            _handle_reading("test_node", raw_payload)

            # Check that dispatch was attempted
            assert mock_dispatch.called

    @pytest.mark.integration
    def test_consecutive_disconnects_handling(self, mqtt_client):
        """L76: repeated disconnects reset readiness/subscription state.

        The old assertion (``isinstance(..., bool)``) was always true and
        verified nothing about the M88/L23 reset behaviour.
        """
        from mqtt.client import _READING_TOPIC, _STATUS_TOPIC

        # Reach a granted/ready state first.
        mqtt_client._pending_subs[1] = _READING_TOPIC
        mqtt_client._pending_subs[2] = _STATUS_TOPIC
        mqtt_client._on_subscribe(mqtt_client._client, None, 1, (1,))
        mqtt_client._on_subscribe(mqtt_client._client, None, 2, (1,))
        assert mqtt_client._ready is True

        # Two consecutive unexpected disconnects must each reset cleanly.
        for rc in (7, 7):
            mqtt_client._on_disconnect(mqtt_client._client, None, rc)
            assert mqtt_client._ready is False
            assert mqtt_client._granted_topics == set()
            assert mqtt_client._pending_subs == {}

    @pytest.mark.integration
    def test_topic_parsing_on_message(self, mqtt_client):
        """Test correct topic extraction from incoming messages."""
        topics = [
            ("air/node/sensor_1/reading", "sensor_1", "reading"),
            ("air/node/sensor_2/status", "sensor_2", "status"),
        ]

        for topic, expected_node, expected_kind in topics:
            match = _TOPIC_RE.fullmatch(topic)
            assert match, f"Topic {topic} should match pattern"
            assert match.group("node_id") == expected_node
            assert match.group("kind") == expected_kind

    @pytest.mark.integration
    def test_raw_payload_truncation_logging(self, mqtt_client, caplog):
        """L77: oversized invalid payloads are truncated in logs, never full.

        The old test had no assertions, so it could not detect whether the
        payload was logged in full or truncated.
        """
        large_payload = "{" + '"x":' * 500 + "}"  # ~3000 chars, invalid JSON

        with caplog.at_level("DEBUG"):
            _handle_reading("test_node", large_payload)

        drop_lines = [
            r.getMessage() for r in caplog.records
            if "Dropping invalid JSON" in r.getMessage()
        ]
        assert drop_lines, "expected the invalid payload to be dropped and logged"
        logged = drop_lines[0]
        assert large_payload not in logged  # never logged in full
        assert "...<" in logged and "more>" in logged  # truncation marker


class TestMQTTClientLifecycle:
    """Test overall MQTT client startup/shutdown behavior."""

    def test_tls_failure_raises_on_start(self):
        """Test that TLS misconfiguration raises on client initialization."""
        with patch("mqtt.client.get_config") as mock_config:
            cfg = MagicMock()
            cfg.MQTT_USE_TLS = True
            cfg.MQTT_TLS_CERT = "/nonexistent/cert.pem"
            cfg.MQTT_TLS_KEY = "/nonexistent/key.pem"
            cfg.MQTT_CA_CERTS = "/nonexistent/ca.pem"
            mock_config.return_value = cfg

            with pytest.raises(RuntimeError, match="MQTT TLS requested"):
                MQTTClient()

    @pytest.mark.integration
    def test_multiple_start_attempts(self, mqtt_client):
        """L76: a second start() while the worker is running is a no-op.

        The old assertion (``isinstance(..., bool)``) was always true; a
        duplicate start() used to spawn a second worker thread.
        """
        with patch.object(mqtt_client._client, "connect_async"), \
             patch.object(mqtt_client._client, "loop_start"):
            mqtt_client.start()
            first_worker = mqtt_client._worker_thread
            assert first_worker is not None and first_worker.is_alive()

            mqtt_client.start()  # must be ignored, not duplicate the worker

            assert mqtt_client._worker_thread is first_worker

    @pytest.mark.integration
    def test_status_heartbeat_updates_user(self):
        """M110: the REAL _handle_status updates Node.last_seen in the DB.

        The old test patched _handle_status, called the mock, and asserted
        the mock was called — it never exercised production code.
        """
        import secrets

        from sqlalchemy import delete, select

        from models import Node
        from models.base import get_sync_db
        from mqtt.client import _handle_status

        node_id = f"HB-{secrets.token_hex(3).upper()}"
        with get_sync_db() as session:
            session.add(Node(
                node_id=node_id, name="heartbeat test", location_name="Test Lab",
                lat=0.0, lon=0.0, reading_interval=30, is_active=True,
            ))

        try:
            with get_sync_db() as session:
                before = session.scalar(
                    select(Node.last_seen).where(Node.node_id == node_id)
                )

            _handle_status(node_id, '{"online": true}')

            with get_sync_db() as session:
                after = session.scalar(
                    select(Node.last_seen).where(Node.node_id == node_id)
                )
            assert after is not None, "online heartbeat must stamp last_seen"
            assert before is None or after > before
        finally:
            with get_sync_db() as session:
                session.execute(delete(Node).where(Node.node_id == node_id))

    @pytest.mark.integration
    def test_enqueuing_message_on_full_queue(self, mqtt_client, caplog):
        """M111: a persistently-full queue drops, warns, and bumps the
        overflow counter surfaced by /admin/health (M37/M38).

        The old test had no assertions.
        """
        from mqtt.client import get_queue_overflow_count

        # Fill the queue
        for _ in range(mqtt_client._queue.maxsize):
            try:
                mqtt_client._queue.put_nowait(("node", "reading", "payload"))
            except queue.Full:
                break

        size_before = mqtt_client._queue.qsize()
        overflow_before = get_queue_overflow_count()

        # Attempt to add one more — persists through all bounded retries
        with caplog.at_level("WARNING"):
            mqtt_client._enqueue("test_node", "reading", "payload")

        assert mqtt_client._queue.qsize() == size_before  # still full
        assert "Ingestion queue full" in caplog.text
        assert get_queue_overflow_count() == overflow_before + 1


class TestMQTTConfigLimits:
    """Test MQTT client configuration and limits."""

    def test_queue_size_limit(self):
        """Test that queue respects size limit."""
        client = MQTTClient()

        # Fill queue
        for _ in range(client._queue.maxsize):
            client._queue.put_nowait(("node", "test", "payload"))

        # Next enqueue should be blocked
        with pytest.raises(queue.Full):
            client._queue.put_nowait(("node", "test", "payload"))

        client.stop()

    def test_reconnect_delay_configuration(self):
        """Test that paho client reconnect delay is set correctly.

        M108: real assertions — the old ``... or True`` could never fail.
        """
        client = MQTTClient()
        assert client._client._reconnect_min_delay == 1
        assert client._client._reconnect_max_delay == 60
        client.stop()

    def test_client_id_is_fixed(self):
        """H36: client ID is the stable per-host prefix + hostname suffix.

        An explicit MQTT_CLIENT_ID wins; otherwise the id is derived as
        ``empyrean-backend-<hostname>`` so two hosts never share a broker
        session while one host keeps the same id across restarts
        (clean_session=False offline queue depends on that stability).
        """
        assert _CLIENT_ID == "empyrean-backend"
        client = MQTTClient()
        cid = client._client._client_id
        if isinstance(cid, bytes):
            cid = cid.decode()
        assert cid.startswith("empyrean-backend")
        client.stop()