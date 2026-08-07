"""
Fire-and-forget MQTT publisher for the Celery worker.

The API process runs its own long-lived ingestion client (``mqtt/client.py``);
the Celery worker is a separate process and builds its own short-lived paho
client **lazily** (module-level singleton per worker process) to publish
threshold-breach alerts to ``air/alerts``. Fail-open by design: a broker outage
is logged, never raised, so a beat task can never fail on a publish.
"""

from __future__ import annotations

import json
import logging
import threading

import paho.mqtt.client as mqtt

from config import get_config

logger = logging.getLogger("empyrean.mqtt")

_ALERTS_TOPIC = "air/alerts"
_QOS = 1
_CLIENT_ID = "empyrean-alert-publisher"

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
            c.connect(cfg.MQTT_BROKER_HOST, cfg.MQTT_BROKER_PORT, keepalive=60)
            c.loop_start()
            _client = c
        except Exception:
            logger.exception("Failed to create alert publisher — disabling")
            _client = None
    return _client


def publish_alert(
    node_id: str, aqi: float, category: str | None, severity: str, timestamp: str
) -> None:
    """Publish ``{node_id, aqi, category, timestamp}`` (+severity) to ``air/alerts``.

    Best-effort: never raises to the caller. A down broker is logged; the alert
    row is committed independently by the caller.
    """
    with _lock:
        client = _get_client()
        if client is None:
            logger.warning("No MQTT publisher available — dropping air/alerts publish for %s", node_id)
            return
        payload = json.dumps(
            {"node_id": node_id, "aqi": aqi, "category": category,
             "severity": severity, "timestamp": timestamp}
        )
        info = client.publish(_ALERTS_TOPIC, payload, qos=_QOS)
        if info.rc != 0:
            logger.warning("air/alerts publish rc=%s for %s", info.rc, node_id)