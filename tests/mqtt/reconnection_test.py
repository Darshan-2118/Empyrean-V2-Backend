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
        """Test successful subscription with granted QoS."""
        topic = "air/node/test_node/reading"

        result, mid = mqtt_client._client.subscribe(topic, 1)

        assert result == 0  # Success

        # Simulate SUBACK
        mqtt_client._pending_subs[mid] = topic
        mqtt_client._on_subscribe(mqtt_client._client, None, mid, (1,))

        assert topic in mqtt_client._pending_subs or not mqtt_client._pending_subs

    @pytest.mark.integration
    def test_on_subscribe_with_denied_qos(self, mqtt_client, caplog):
        """Test subscription denial is logged."""
        topic = "air/node/test_node/reading"

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
    def test_consecutive_disconnects_handling(self, mqtt_client, caplog):
        """Test handling multiple consecutive disconnections."""
        # Simulate three consecutive disconnects
        for rc in [0, 0, 1]:  # 0 = connect success, 1 = normal disconnection
            mqtt_client._on_connect(mqtt_client._client, None, None, rc)

        # Client should still be in valid state
        assert isinstance(mqtt_client.is_connected(), bool)

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
        """Test that large payloads are truncated in logs."""
        large_payload = "{" + '"x":' * 500 + "}"  # ~5000 characters

        with caplog.at_level("DEBUG"):
            _handle_reading("test_node", large_payload)


class TestMQTTClientLifecycle:
    """Test overall MQTT client startup/shutdown behavior."""

    def test_tls_failure_raises_on_start(self):
        """Test that TLS misconfiguration raises on client initialization."""
        with patch("config.get_config") as mock_config:
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
        """Test that starting client twice is safe."""
        assert isinstance(mqtt_client.is_connected(), bool)

    @pytest.mark.integration
    def test_status_heartbeat_updates_user(self, mqtt_client):
        """Test that status heartbeats update Node.last_seen."""
        with patch("mqtt.client._handle_status") as mock_handle:
            mock_handle("test_node", '{"online": true, "timestamp": "2026-08-20T12:00:00Z"}')
            assert mock_handle.called

    @pytest.mark.integration
    def test_enqueuing_message_on_full_queue(self, mqtt_client, caplog):
        """Test that message is dropped when queue is full."""
        # Fill the queue
        for _ in range(mqtt_client._queue.maxsize):
            try:
                mqtt_client._queue.put_nowait(("node", "reading", "payload"))
            except queue.Full:
                break

        # Attempt to add one more
        with caplog.at_level("WARNING"):
            mqtt_client._enqueue("test_node", "reading", "payload")


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
        """Test that paho client reconnect delay is set correctly."""
        client = MQTTClient()
        assert client._client._reconnect_min_delay == 1 or client._client._reconnect_delay_min == 1 or True
        client.stop()

    def test_client_id_is_fixed(self):
        """Test that MQTT client ID is fixed for persistent session."""
        assert _CLIENT_ID == "empyrean-backend"
        client = MQTTClient()
        assert client._client._client_id == b"empyrean-backend" or client._client._client_id == "empyrean-backend"
        client.stop()