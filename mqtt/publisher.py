"""
Fire-and-forget MQTT publisher for the Celery worker.

The API process runs its own long-lived ingestion client (``mqtt/client.py``);
the Celery worker is a separate process and builds its own short-lived paho
client **lazily** (module-level singleton per worker process) to publish
threshold-breach alerts to ``air/alerts``. Fail-open by design: a broker outage
is logged, never raised, so a beat task can never fail on a publish.

Issue #25: Failed publishes are queued for bounded retry to prevent data loss
during transient broker outages. M100: the retry path never sleeps in the
calling beat task, processes a bounded batch per invocation, and re-enqueues
anything not published so an interrupted retry cannot lose messages.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt

from config import get_config

logger = logging.getLogger("empyrean.mqtt")

_ALERTS_TOPIC = "air/alerts"
_QOS = 1
# M99: prefix only — the runtime id appends hostname + PID (mirroring H36 in
# mqtt/client.py) so prefork worker processes never connect with the same
# client id and drop each other's broker session on every CONNECT.
_CLIENT_ID = "empyrean-alert-publisher"

# Retry configuration (L21: these knobs are publisher-local on purpose).
# This deque-based retry is the *only* retry mechanism for alert publishes:
# the Celery side (tasks/alerts.py) calls publish_alert() fire-and-forget and
# does not re-dispatch on publish failure, so there is no second, divergent
# retry strategy to reconcile with.
_MAX_RETRY_ATTEMPTS = 5
# M100: per-call retry budget — at most this many queued alerts are retried
# per invocation, leaving the rest in the deque for the next call so the
# calling beat task is never blocked draining the queue.
_RETRY_BATCH_SIZE = 10

# Queue for failed messages.
# M40: this is a per-worker-process module-level deque — Celery workers do
# NOT coordinate retries across processes. A worker that dies with queued
# alerts loses them (bounded by design: alerts are best-effort notifications
# and the underlying alert rows are committed to the DB independently).
# Moving the queue to Redis would add cross-worker durability at the cost of
# a Redis dependency on the publish path; deliberately not done.
_MAX_FAILED_QUEUE_SIZE = 100
_failed_queue: deque[tuple[dict[str, Any], int]] = deque(maxlen=_MAX_FAILED_QUEUE_SIZE)  # (payload, attempt_count)

_lock = threading.Lock()
_client: mqtt.Client | None = None

# Ports that conventionally require TLS. Publishing plaintext to one of these
# is almost certainly a misconfiguration (H20).
_TLS_PORTS = {8883, 8884}


def _broker_requires_tls() -> bool:
    """Heuristic: the configured broker port is a TLS-only port."""
    cfg = get_config()
    return cfg.MQTT_BROKER_PORT in _TLS_PORTS


def _get_client() -> mqtt.Client | None:
    """Return a lazily-created paho client, or ``None`` if construction fails.

    H20: fail-closed on TLS misconfiguration. If the broker requires TLS but
    this process's certs are unset, refuse to construct a plaintext client —
    the old behaviour silently published in plaintext from a misconfigured
    worker.
    """
    global _client
    if _client is None:
        cfg = get_config()
        try:
            # M99: unique per host + worker process (hostname/PID suffix, as
            # H36 does for the ingestion client) — a shared fixed id made
            # prefork workers takeover each other's broker session.
            client_id = f"{_CLIENT_ID}-{socket.gethostname().lower()}-{os.getpid()}"
            c = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
            if cfg.MQTT_USE_TLS:
                if not all((cfg.MQTT_CA_CERTS, cfg.MQTT_TLS_CERT, cfg.MQTT_TLS_KEY)):
                    logger.error("MQTT TLS requested but certs unset — alerts publish disabled")
                    return None
                c.tls_set(ca_certs=cfg.MQTT_CA_CERTS, certfile=cfg.MQTT_TLS_CERT, keyfile=cfg.MQTT_TLS_KEY)
            elif _broker_requires_tls():
                # H20: broker expects TLS (port 8883 or config says so) but this
                # process has TLS disabled — never publish in plaintext.
                logger.error(
                    "MQTT_USE_TLS disabled but broker appears to require TLS "
                    "(port %s) — refusing to publish in plaintext",
                    cfg.MQTT_BROKER_PORT,
                )
                return None
            # M41: connect_async + loop_start — the old synchronous connect()
            # blocked the calling Celery task for the full TCP handshake
            # timeout against a slow/unreachable broker. Connection state is
            # handled by the is_connected() check in publish_alert(), which
            # queues messages until the (auto-reconnecting) client is up.
            c.connect_async(cfg.MQTT_BROKER_HOST, cfg.MQTT_BROKER_PORT, keepalive=60)
            c.loop_start()
            _client = c
            logger.debug(
                "MQTT client connecting (async) to %s:%s",
                cfg.MQTT_BROKER_HOST, cfg.MQTT_BROKER_PORT,
            )
        except Exception:
            logger.exception("Failed to create alert publisher — disabling")
            _client = None
    return _client


def _retry_failed_messages() -> None:
    """Retry a bounded batch of queued failed messages (Issue #25).

    M100: never sleeps in the caller's thread and processes at most
    ``_RETRY_BATCH_SIZE`` messages per invocation, leaving the rest in the
    deque for the next call. The batch is popped under the lock (not
    snapshot-and-cleared) and anything not successfully published is
    re-enqueued in the ``finally``, so a mid-retry exception (e.g. Celery
    ``SoftTimeLimitExceeded``) can never lose the outstanding batch.
    """
    if not _failed_queue:
        return

    client = _get_client()
    if client is None:
        return  # Can't retry without a client

    with _lock:
        batch: list[tuple[dict[str, Any], int]] = []
        while _failed_queue and len(batch) < _RETRY_BATCH_SIZE:
            batch.append(_failed_queue.popleft())

    try:
        while batch:
            payload, attempt = batch.pop(0)
            if attempt >= _MAX_RETRY_ATTEMPTS:
                logger.error("Max retry attempts reached for alert to node %s — dropping", payload.get("node_id", "unknown"))
                continue

            encoded = _encode(payload)
            if encoded is None:
                continue  # H22: unencodable payloads are dropped, not retried

            try:
                info = client.publish(_ALERTS_TOPIC, encoded, qos=_QOS)
            except Exception:
                # M100: re-enqueue the in-flight message, then let the finally
                # re-enqueue the rest of the batch before propagating.
                logger.exception("Retry publish raised for alert to node %s — requeueing", payload.get("node_id", "unknown"))
                with _lock:
                    _failed_queue.append((payload, attempt + 1))
                raise

            if info.rc == 0:
                logger.info("Retry successful for alert to node %s (attempt %d)", payload.get("node_id", "unknown"), attempt + 1)
            else:
                logger.warning("Retry failed (rc=%s) for alert to node %s (attempt %d)", info.rc, payload.get("node_id", "unknown"), attempt + 1)
                with _lock:
                    _failed_queue.append((payload, attempt + 1))
    finally:
        if batch:
            with _lock:
                _failed_queue.extendleft(reversed(batch))


def _encode(payload: dict[str, Any]) -> str | None:
    """JSON-encode an alert payload, returning ``None`` on failure (H22).

    A non-serializable value must never raise out of the publisher — the
    caller is a best-effort fire-and-forget path and a crash here would take
    down the calling beat task instead of just dropping one alert.
    """
    try:
        return json.dumps(payload)
    except (TypeError, ValueError) as exc:
        logger.error("Alert payload not JSON-serializable — dropped: %s", exc)
        return None


def publish_alert(
    node_id: str, aqi: float, category: str | None, severity: str, timestamp: str
) -> None:
    """Publish ``{node_id, aqi, category, timestamp}`` (+severity) to ``air/alerts``.

    Best-effort: never raises to the caller. A down broker is logged; the alert
    row is committed independently by the caller.

    Issue #25: Failed publishes are queued for bounded retry to prevent data
    loss during transient broker outages.
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

        # H22: encode under the guard — a bad payload is dropped with an error
        # log instead of crashing the publishing task.
        encoded = _encode(payload)
        if encoded is None:
            published = False
        else:
            info = client.publish(_ALERTS_TOPIC, encoded, qos=_QOS)
            if info.rc != 0:
                logger.warning("air/alerts publish rc=%s for %s — queueing for retry", info.rc, node_id)
                _queue_failed_message(node_id, aqi, category, severity, timestamp)
                return

            published = True

    # Retry queued messages OUTSIDE the lock to avoid blocking other publishers.
    # M100: the retry must never escape publish_alert's "never raises" contract
    # — an interrupted batch has already re-enqueued itself in _retry's finally.
    if published:
        try:
            _retry_failed_messages()
        except Exception:
            logger.exception("Alert retry batch interrupted — remaining messages re-queued")


def _queue_failed_message(
    node_id: str, aqi: float, category: str | None, severity: str, timestamp: str
) -> None:
    """Queue a failed message for bounded retry (Issue #25)."""
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