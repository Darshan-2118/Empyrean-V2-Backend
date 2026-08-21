"""
Fire-and-forget MQTT publisher for the Celery worker.

The API process runs its own long-lived ingestion client (``mqtt/client.py``);
the Celery worker is a separate process and builds its own short-lived paho
client **lazily** (module-level singleton per worker process) to publish
threshold-breach alerts to ``air/alerts``. Fail-open by design: a broker outage
is logged, never raised, so a beat task can never fail on a publish.

Issue #25: Added exponential backoff retry for publish failures with a queue
for failed messages to prevent data loss during transient broker outages.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt

from config import get_config

logger = logging.getLogger("empyrean.mqtt")

_ALERTS_TOPIC = "air/alerts"
_QOS = 1
_CLIENT_ID = "empyrean-alert-publisher"

# Retry configuration
_MAX_RETRY_ATTEMPTS = 5
_BASE_RETRY_DELAY = 1.0  # seconds
_MAX_RETRY_DELAY = 30.0  # seconds
_RETRY_JITTER = 0.1  # 10% jitter

# Queue for failed messages
_MAX_FAILED_QUEUE_SIZE = 100
_failed_queue: deque[tuple[dict[str, Any], int]] = deque(maxlen=_MAX_FAILED_QUEUE_SIZE)  # (payload, attempt_count)

_lock = threading.Lock()
_client: mqtt.Client | None = None


def _get_client() -> mqtt.Client | None:
    """Return a lazily-created paho client, or ``None`` if construction fails."""
    global _client
    if _client is None:
        cfg = get_config()
        try:
            c = mqtt.Client(client_id=_CLIENT_ID, protocol=mqtt.MQTTv311)
            if cfg.MQTT_USE_TLS:
                if not all((cfg.MQTT_CA_CERTS, cfg.MQTT_TLS_CERT, cfg.MQTT_TLS_KEY)):
                    logger.error("MQTT TLS requested but certs unset — alerts publish disabled")
                    return None
                c.tls_set(ca_certs=cfg.MQTT_CA_CERTS, certfile=cfg.MQTT_TLS_CERT, keyfile=cfg.MQTT_TLS_KEY)
            rc = c.connect(cfg.MQTT_BROKER_HOST, cfg.MQTT_BROKER_PORT, keepalive=60)
            if rc != 0:
                logger.error("MQTT connect failed (rc=%s) — disabling publisher", rc)
                logger.error("Connection error: incomplete use of protocol, client is illegible or revoked")
                return None
            logger.debug("MQTT client connected to %s:%s (rc=%s)", cfg.MQTT_BROKER_HOST, cfg.MQTT_BROKER_PORT, rc)
            c.loop_start()
            _client = c
        except Exception:
            logger.exception("Failed to create alert publisher — disabling")
            _client = None
    return _client


def _calculate_retry_delay(attempt: int) -> float:
    """Calculate exponential backoff delay with jitter."""
    import random
    delay = min(_BASE_RETRY_DELAY * (2 ** attempt), _MAX_RETRY_DELAY)
    # Add jitter: ±10%
    jitter = delay * _RETRY_JITTER * (2 * random.random() - 1)
    return delay + jitter


def _retry_failed_messages() -> None:
    """Retry failed messages from the queue with exponential backoff.

    Collects messages to retry under the lock, then retries them outside the
    lock so time.sleep() does not block other publishers.
    """
    global _failed_queue
    if not _failed_queue:
        return

    client = _get_client()
    if client is None:
        return  # Can't retry without a client

    # Snapshot and clear under the lock, then retry without holding it
    with _lock:
        to_retry = list(_failed_queue)
        _failed_queue.clear()

    still_failed: list[tuple[dict[str, Any], int]] = []
    for payload, attempt in to_retry:
        if attempt >= _MAX_RETRY_ATTEMPTS:
            logger.error("Max retry attempts reached for alert to node %s — dropping", payload.get("node_id", "unknown"))
            continue

        delay = _calculate_retry_delay(attempt)
        time.sleep(delay)

        info = client.publish(_ALERTS_TOPIC, json.dumps(payload), qos=_QOS)
        if info.rc == 0:
            logger.info("Retry successful for alert to node %s (attempt %d)", payload.get("node_id", "unknown"), attempt + 1)
        else:
            logger.warning("Retry failed (rc=%s) for alert to node %s (attempt %d)", info.rc, payload.get("node_id", "unknown"), attempt + 1)
            still_failed.append((payload, attempt + 1))

    # Re-enqueue any that still failed
    if still_failed:
        with _lock:
            for item in still_failed:
                _failed_queue.append(item)


def publish_alert(
    node_id: str, aqi: float, category: str | None, severity: str, timestamp: str
) -> None:
    """Publish ``{node_id, aqi, category, timestamp}`` (+severity) to ``air/alerts``.

    Best-effort: never raises to the caller. A down broker is logged; the alert
    row is committed independently by the caller.

    Issue #25: Failed publishes are queued for exponential backoff retry to
    prevent data loss during transient broker outages.
    """
    published = False
    with _lock:
        client = _get_client()
        if client is None:
            logger.warning("No MQTT publisher available — queueing air/alerts publish for %s", node_id)
            _queue_failed_message(node_id, aqi, category, severity, timestamp)
            return

        # Check if client is connected before publishing
        if not client.is_connected():
            logger.warning("MQTT client not connected (rc=%s) — queueing air/alerts publish for %s",
                          client.is_connected(), node_id)
            _queue_failed_message(node_id, aqi, category, severity, timestamp)
            return

        payload = {"node_id": node_id, "aqi": aqi, "category": category,
                   "severity": severity, "timestamp": timestamp}

        info = client.publish(_ALERTS_TOPIC, json.dumps(payload), qos=_QOS)
        if info.rc != 0:
            logger.warning("air/alerts publish rc=%s for %s — queueing for retry", info.rc, node_id)
            _queue_failed_message(node_id, aqi, category, severity, timestamp)
            return

        published = True

    # Retry queued messages OUTSIDE the lock to avoid blocking other publishers
    if published:
        _retry_failed_messages()


def _queue_failed_message(
    node_id: str, aqi: float, category: str | None, severity: str, timestamp: str
) -> None:
    """Queue a failed message for retry with exponential backoff."""
    payload = {"node_id": node_id, "aqi": aqi, "category": category,
               "severity": severity, "timestamp": timestamp}

    if len(_failed_queue) >= _MAX_FAILED_QUEUE_SIZE:
        # Drop oldest message if queue is full
        dropped = _failed_queue.popleft()
        logger.error("Failed message queue full — dropping oldest alert for node %s", dropped[0].get("node_id", "unknown"))

    _failed_queue.append((payload, 0))  # (payload, attempt_count)
    logger.info("Queued failed alert for node %s (queue size: %d)", node_id, len(_failed_queue))


def get_publisher_stats() -> dict:
    """Get publisher statistics for monitoring (Issue #25)."""
    with _lock:
        return {
            "client_connected": _client is not None and _client.is_connected(),
            "failed_queue_size": len(_failed_queue),
            "max_queue_size": _MAX_FAILED_QUEUE_SIZE,
            "max_retry_attempts": _MAX_RETRY_ATTEMPTS,
        }