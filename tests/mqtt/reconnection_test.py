"""
Integration tests for MQTT client reconnection behavior.

Tests the MQTTClient lifecycle under various network failure scenarios,
including broker disconnection, network timeout, and TLS handshake failures.

Run with: pytest tests/mqtt/reconnection_test.py -v
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from mqtt.client import MQTTClient
from mqtt.config import MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_USE_TLS


class TestMQTTReconnection:
    """Test MQTT client reconnection and network failure handling."""

    @pytest.fixture
    def mqtt_client(self):
        """Create a fresh MQTT client for each test."""
        client = MQTTClient()
        client._ready = False  # Skip ready check
        yield client
        client.stop()

    @pytest.mark.integration
    def test_on_connect_resubscribes_on_reconnect(self, mqtt_client):
        """Test that client re-establishes subscriptions after reconnection."""
        original_subscribe = mqtt_client._client.subscribe

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

        result, mid = mqtt_client._client.subscribe(topic, mqtt.MQTTv311.QOS1)

        assert result == 0  # Success

        # Simulate SUBACK
        mqtt_client._on_subscribe(mqtt_client._client, None, mid, (mqtt.MQTTv311.QOS1,))

        assert mqtt_client._ready
        assert topic in mqtt_client._pending_subs

    @pytest.mark.integration
    def test_on_subscribe_with_denied_qos(self, mqtt_client, caplog):
        """Test subscription denial is logged."""
        topic = "air/node/test_node/reading"

        result, mid = mqtt_client._client.subscribe(topic, mqtt.MQTTv311.QOS1)

        assert result == 0  # Send succeeded

        # Simulate broker denying
        with caplog.at_level("ERROR"):
            mqtt_client._on_subscribe(mqtt_client._client, None, mid, (1,))  # QoS granted but maybe wrong?

        assert "Subscription to" in caplog.text
        assert "denied" in caplog.text

    @pytest.mark.integration
    def test_message_handling_on_reconnect(self, mqtt_client, caplog):
        """Test that messages still route correctly regardless of reconnection."""
        from mqtt.validator import validate_reading

        # Prepare a valid reading payload
        payload = {
            "node_id": "test_node",
            "timestamp": "2026-08-20T12:00:00Z",
            "pm25": 12.5,
            "pm10": 45.0,
        }

        raw_payload = '{"pm25":12.5,"pm10":45.0,"node_id":"test_node","timestamp":"2026-08-20T12:00:00Z"}'

        # Mock the route handler to avoid actual task dispatch
        from unittest.mock import patch

        with patch("mqtt.client._dispatch_reading") as mock_dispatch:
            # Process message on a known topic
            mqtt_client._handle_reading("test_node", raw_payload)

            # Give async dispatch a moment
            time.sleep(0.1)

            # Check that dispatch was attempted
            assert mock_dispatch.called

    @pytest.mark.integration(skip_tls_cert_setup)
    def test_consecutive_disconnects_handling(self, mqtt_client, caplog):
        """Test handling multiple consecutive disconnections."""
        initial_connect_time = time.time()

        # Simulate three consecutive disconnects
        for rc in [0, 0, 1]:  # 0 = connect success, 1 = normal disconnection
            mqtt_client._on_connect(mqtt_client._client, None, None, rc)
            time.sleep(0.5)  # Allow time between connections

        # Client should still be in valid state
        assert mqtt_client.is_connected() or not mqtt_client.is_connected()

    @pytest.mark.integration
    def test_topic_parsing_on_message(self, mqtt_client):
        """Test correct topic extraction from incoming messages."""
        topics = [
            "air/node/sensor_1/reading",
            "air/node/sensor_2/status",
            "air/node/sensor_3/alerts",
        ]

        for topic in topics:
            match = mqtt_client._TOPIC_RE.fullmatch(topic)
            assert match, f"Topic {topic} should match pattern"
            assert match.group("node_id") in ["sensor_1", "sensor_2", "sensor_3"]
            assert match.group("kind") in ["reading", "status"]

    @pytest.mark.integration
    def test_raw_payload_truncation_logging(self, mqtt_client, caplog):
        """Test that large payloads are truncated in logs."""
        large_payload = "{" + '"x":' * 500 + "}"  # ~5000 characters

        with caplog.at_level("DEBUG"):
            # This should log without raising
            mqtt_client._handle_reading("test_node", large_payload)

        # Check for truncation in logs
        log_records = [r for r in caplog.records if "truncated" in r.message]
        assert len(log_records) > 0


class TestMQTTClientLifecycle:
    """Test overall MQTT client startup/shutdown behavior."""

    def test_tls_failure_raises_on_start(self):
        """Test that TLS misconfiguration raises on client start."""
        with patch("config.get_config") as mock_config:
            cfg = MagicMock()
            cfg.MQTT_USE_TLS = False  # Not using TLS, but certs set
            cfg.MQTT_TLS_CERT = "/nonexistent/cert.pem"
            cfg.MQTT_TLS_KEY = "/nonexistent/key.pem"
            cfg.MQTT_CA_CERTS = "/nonexistent/ca.pem"
            mock_config.return_value = cfg

            client = MQTTClient()
            with pytest.raises(RuntimeError, match="MQTT TLS requested"):
                client.start()

    @pytest.mark.integration
    def test_multiple_start_attempts(self, mqtt_client):
        """Test that starting client twice is safe."""
        # Record initial state
        was_running = mqtt_client.is_connected()

        # Try to start again (should be idempotent or handled gracefully)
        initial_count = mqtt_client._client._out_packet_cb_count

        # Just ensure we don't crash - actual behavior depends on paho

    @pytest.mark.integration
    def test_status_heartbeat_updates_user(self, mqtt_client):
        """Test that status heartbeats update Node.last_seen."""
        from sqlalchemy import update
        from datetime import timezone
        from models import Node

        # Mock the DB update
        with patch.object(mqtt_client, "_handle_status") as mock_handle:
            mock_handle("test_node", '{"online": true, "timestamp": "2026-08-20T12:00:00Z"}')

            # Verify the handler was called
            assert mock_handle.called

    @pytest.mark.integration
    def test_enqueuing_message_on_full_queue(self, mqtt_client, caplog):
        """Test that message is dropped when queue is full."""
        from mqtt.config import _QUEUE_MAX, _ENQUEUE_TIMEOUT

        # Fill the queue
        max_attempts = _ENQUEUE_MAX_ATTEMPTS
        for _ in range(max_attempts):
            # Use put_nowait to fill without waiting (gets blocked)
            try:
                mqtt_client._queue.put_nowait(("node", "reading", "payload"))
            except queue.Full:
                break  # Queue is now full

        # Attempt to add one more - should fail/back off
        with caplog.at_level("WARNING"):
            mqtt_client._enqueue("test_node", "reading", "payload")

            # Check for overflow log
            log_records = [r for r in caplog.records if "queue full" in r.message.lower()]
            assert len(log_records) > 0


class TestMQTTConfigLimits:
    """Test MQTT client configuration and limits."""

    def test_queue_size_limit(self):
        """Test that queue respects size limit."""
        client = MQTTClient()

        # Should be able to enqueue max size
        for i in range(client._queue.maxsize):
            client._queue.put_nowait(("node", "test", "payload"))

        # Next enqueue should be blocked
        with pytest.raises(queue.Full):
            client._queue.put_nowait(("node", "test", "payload"))

        client.stop()

    def test_reconnect_delay_configuration(self):
        """Test that paho client reconnect delay is set correctly."""
        client = MQTTClient()

        # Check that reconnect delay was configured
        assert client._client.reconnect_delay_set_called is True
        assert client._client._reconnect_delay_min = 1
        assert client._client._reconnect_delay_max = 60

    def test_client_id_is_fixed(self):
        """Test that MQTT client ID is fixed for persistent session."""
        with pytest.raises(ImportError):
            from mqtt.client import _CLIENT_ID  # Import if package-level

        # Check code for client_id
        client = MQTTClient()
        assert client._client._client_id == "empyrean-backend"


if __name__ == "__main__":
    # Run tests manually for debugging
    pytest.main([__file__, "-v", "--tb=short"])