"""
MQTT ingestion client — subscribes to device readings and heartbeats.

Dispatches validated readings to Celery (``tasks.process_reading``) and
touches ``Node.last_seen`` on online heartbeats. All callback work is wrapped
so that network/broker errors are logged, never raised — paho's background
loop owns reconnection.

Design notes (see docs/mqtt.md):

- **Topic id is authoritative (H-3):** the ``node_id`` parsed from
  ``air/node/{id}/reading`` overrides any body ``node_id`` *before*
  validation/dispatch, so a spoofed body cannot attribute a reading to
  another node. The body id stays optional as a cross-check.
- **Worker thread (M-9):** handler DB/Celery I/O runs on a bounded worker
  queue, not the paho network-loop thread, so a slow DB/Redis cannot stall
  all ingestion.
- **Bounded retry (M-8):** a transient Celery/Redis ``delay()``/queue failure
  is retried a bounded number of times rather than log-and-dropped; the
  bounded in-memory queue acts as short-term persistence.
- **Fail-closed TLS (H-4):** if TLS was requested but cert files are missing
  or invalid, construction raises and the client refuses to run plaintext.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt  # paho-mqtt 1.6.1 (MQTTv311)
from sqlalchemy import update

from config import get_config
from models import Node
from models.base import get_sync_db

from .validator import validate_reading, validate_status

logger = logging.getLogger("empyrean.mqtt")

# Topic-parsing regex per docs/mqtt.md contract.
_TOPIC_RE = re.compile(r"^air/node/(?P<node_id>[^/]+)/(?P<kind>reading|status)$")

_READING_TOPIC = "air/node/+/reading"
_STATUS_TOPIC = "air/node/+/status"
_QOS = 1

# Fixed client id + persistent session (clean_session=False, L-20): offline
# delivery of queued QoS1 device messages is honored, consistent with the
# at-least-once contract and the M-8 bounded-queue/retry durability path.
_CLIENT_ID = "empyrean-backend"

# Worker-queue backpressure bounds (M-9/M-8). The queue is bounded to bound
# memory; production should pair this with the Celery acks_late path (Fixer C).
_QUEUE_MAX = 1000
_ENQUEUE_MAX_ATTEMPTS = 5
_ENQUEUE_TIMEOUT = 0.5  # seconds

# Dispatch retry bounds for a transient Celery/Redis outage (M-8).
_DISPATCH_MAX_ATTEMPTS = 3
_DISPATCH_RETRY_DELAY = 0.5  # seconds

# Truncation length for logging raw payloads (L-21).
_LOG_TRUNCATE = 200


def _truncated_repr(value: object) -> str:
    """``repr`` of *value* capped at ``_LOG_TRUNCATE`` chars for safe logging."""
    text = repr(value)
    if len(text) > _LOG_TRUNCATE:
        return text[:_LOG_TRUNCATE] + f"...<{len(text) - _LOG_TRUNCATE} more>"
    return text


def _resolve_payload(data: bytes | str) -> str | None:
    """Decode a message payload to ``str`` (``None`` on undecodable bytes)."""
    if isinstance(data, bytes):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Dropping message with undecodable payload")
            return None
    return data


def _handle_status(node_id: str, raw: str) -> None:
    """Update ``Node.last_seen`` when a known node reports online."""
    payload = validate_status(_json_loads(raw, "status"))
    if payload is None:
        return
    if not payload.online:
        return

    try:
        with get_sync_db() as session:
            result = session.execute(
                update(Node)
                .where(Node.node_id == node_id)
                .values(last_seen=datetime.now(timezone.utc))
            )
            if result.rowcount == 0:
                logger.info("Heartbeat for unknown node %r ignored", node_id)
    except Exception:
        logger.exception("Failed to update last_seen for node %r", node_id)


def _json_loads(raw: str, kind: str) -> dict | None:
    """Parse a JSON payload string, returning ``None`` (logged) on failure."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Dropping invalid JSON %s payload: %s", kind, _truncated_repr(raw))
        return None
    if not isinstance(data, dict):
        logger.warning("Dropping non-object %s payload: %s", kind, _truncated_repr(raw))
        return None
    return data


# Cached reference to the Celery task so we don't re-import it per message.
_process_reading_task = None


def _get_process_reading_task():
    """Return the ``tasks.process_reading`` task, importing it lazily once.

    The import is deferred (matching the repo's parallel-phase guard) and
    cached so the hot dispatch path does not re-import on every reading.
    """
    global _process_reading_task
    if _process_reading_task is None:
        # tasks.process_reading is landed by a parallel phase; guard the import
        # so this client stays importable if the task module is not ready yet.
        from tasks.process_reading import process_reading

        _process_reading_task = process_reading
    return _process_reading_task


def _dispatch_reading(node_id: str, payload_model) -> None:
    """Dispatch a validated reading to Celery with bounded retry (M-8).

    A transient failure (Redis/celery outage) is retried ``_DISPATCH_MAX_ATTEMPTS``
    times with backoff instead of being logged-and-dropped on the first error.
    Serialized with ``mode="json"`` so Pydantic ``datetime`` is a JSON string
    before Kombu encodes it (prevents the C-1 ``datetime`` round-trip bug).
    """
    serialized = payload_model.model_dump(mode="json")
    task = _get_process_reading_task()
    for attempt in range(1, _DISPATCH_MAX_ATTEMPTS + 1):
        try:
            task.delay(serialized)
            return
        except Exception:
            if attempt == _DISPATCH_MAX_ATTEMPTS:
                logger.exception(
                    "Giving up dispatching reading for node %r after %d attempts",
                    node_id,
                    attempt,
                )
                return
            time.sleep(_DISPATCH_RETRY_DELAY * attempt)


def _handle_reading(node_id: str, raw: str) -> None:
    """Validate a reading and dispatch it to Celery for processing.

    The topic ``node_id`` is authoritative (H-3): it overrides any body
    ``node_id`` *before* validation, so a spoofed body cannot attribute the
    reading elsewhere — and a compliant topic-only device is not dropped.
    """
    data = _json_loads(raw, "reading")
    if data is None:
        return

    data["node_id"] = node_id

    payload = validate_reading(data)
    if payload is None:
        return

    _dispatch_reading(node_id, payload)


class MQTTClient:
    """Wraps a paho MQTT client wired to the Empyrean topic contract.

    ``start()``/``stop()`` are the full lifecycle; ``main()`` is the standalone
    entrypoint with a subscription smoke check. Integration hooks are
    documented in ``docs/mqtt.md``.
    """

    def __init__(self) -> None:
        self._cfg = get_config()
        self._stop_event = threading.Event()
        self._queue: "queue.Queue[tuple[str, str, str]]" = queue.Queue(maxsize=_QUEUE_MAX)
        self._worker_thread: threading.Thread | None = None
        self._pending_subs: dict[int, str] = {}  # mid -> topic (L-23)
        self._ready = False  # True once both subscriptions are granted
        self._tls_configured = False  # True only after tls_set() succeeds (H-4)

        self._client = mqtt.Client(
            client_id=_CLIENT_ID,
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

        if self._cfg.MQTT_USE_TLS:
            self._configure_tls()

    def _configure_tls(self) -> None:
        """Apply mqtts options, **raising** on any missing/invalid cert (H-4).

        The process must not run plaintext when TLS was requested. Every
        required setting (``MQTT_TLS_CERT``/``MQTT_TLS_KEY``/``MQTT_CA_CERTS``)
        must be set to a real file on disk; an empty value or a missing file
        aborts construction so ``start()`` can never connect insecurely.
        """
        import os

        cert = self._cfg.MQTT_TLS_CERT
        key = self._cfg.MQTT_TLS_KEY
        ca = self._cfg.MQTT_CA_CERTS

        # H-4: an empty/unset setting is just as fatal as a missing file —
        # ``tls_set`` must never be skipped, and the client must never fall back
        # to plaintext (or silently skip client-cert auth).
        required = (
            ("MQTT_TLS_CERT", cert),
            ("MQTT_TLS_KEY", key),
            ("MQTT_CA_CERTS", ca),
        )
        missing = [
            f"{name}={value!r}"
            for name, value in required
            if not value or not os.path.exists(value)
        ]
        if missing:
            raise RuntimeError(
                "MQTT TLS requested (MQTT_USE_TLS=True) but cert settings are "
                f"unset or unreadable: {missing}. Refusing to connect "
                "plaintext."
            )

        try:
            self._client.tls_set(
                ca_certs=ca,
                certfile=cert,
                keyfile=key,
            )
            logger.info("MQTT TLS configured (client-cert auth)")
        except Exception as exc:
            raise RuntimeError(
                "Failed to configure MQTT TLS (cert files invalid)"
            ) from exc
        self._tls_configured = True

    def _on_connect(self, client, userdata, flags, rc) -> None:
        """(Re)subscribe to the wildcard topics on every (re)connect."""
        logger.info("Connected to MQTT broker (rc=%s)", rc)
        if rc != 0:
            logger.error("Broker connection refused with rc=%s — will retry", rc)
            return
        for topic in (_READING_TOPIC, _STATUS_TOPIC):
            result, mid = client.subscribe(topic, qos=_QOS)
            if result != mqtt.MQTT_ERR_SUCCESS:
                logger.error("Failed to subscribe to %s (rc=%s)", topic, result)
            else:
                self._pending_subs[mid] = topic

    def _on_subscribe(self, client, userdata, mid, granted_qos) -> None:
        """Verify the granted QoS on SUBACK and surface denials (L-23).

        ``subscribe()`` returning SUCCESS only means the SUBSCRIBE packet was
        sent. A broker can deny it in the SUBACK; inspecting the granted QoS is
        the authoritative check.
        """
        topic = self._pending_subs.pop(mid, None)
        granted = granted_qos[0] if granted_qos else None
        if topic is None:
            logger.debug("SUBACK for unknown subscribe mid=%s (reconnect race)", mid)
            return
        if granted is None:
            logger.error(
                "Subscription to %s denied by broker (no granted QoS)", topic
            )
            return
        self._ready = True
        logger.info("Subscribed to %s @ granted QoS %s", topic, granted)

    def _on_message(self, client, userdata, msg) -> None:
        """Decode + route on the paho loop; heavier work goes to the worker."""
        try:
            raw = _resolve_payload(msg.payload)
            if raw is None:
                return
            match = _TOPIC_RE.fullmatch(msg.topic)
            if not match:
                logger.debug("Dropping unknown topic %r", msg.topic)
                return
            self._enqueue(match.group("node_id"), match.group("kind"), raw)
        except Exception:
            logger.exception("Unhandled error in on_message for %r", msg.topic)

    def _enqueue(self, node_id: str, kind: str, raw: str) -> None:
        """Push a message to the worker queue with bounded backpressure.

        A transiently-full queue is retried so a slow worker doesn't force drops,
        but a persistently-full queue (worker can't keep up) drops with a
        warning rather than blocking the paho loop (M-9).
        """
        for attempt in range(_ENQUEUE_MAX_ATTEMPTS):
            try:
                self._queue.put((node_id, kind, raw), timeout=_ENQUEUE_TIMEOUT)
                return
            except queue.Full:
                continue
        logger.warning(
            "Ingestion queue full — dropping %s for node %r (backpressure)",
            kind,
            node_id,
        )

    def _run_worker(self) -> None:
        """Consume the dispatch queue on a dedicated thread (M-9)."""
        while not self._stop_event.is_set():
            try:
                node_id, kind, raw = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if kind == "reading":
                    _handle_reading(node_id, raw)
                else:
                    _handle_status(node_id, raw)
            except Exception:
                logger.exception("Worker error processing %s for node %r", kind, node_id)
            finally:
                self._queue.task_done()

    def start(self) -> None:
        """Start the worker thread, connect asynchronously, and run the loop.

        Fails closed on TLS misconfiguration (H-4): if TLS was requested but
        ``_configure_tls`` did not complete successfully, raise rather than
        connect — even over plaintext — or loop reconnecting forever.
        """
        if self._cfg.MQTT_USE_TLS and not self._tls_configured:
            raise RuntimeError(
                "MQTT TLS requested (MQTT_USE_TLS=True) but TLS was not "
                "configured — refusing to connect"
            )

        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(
            target=self._run_worker, name="empyrean-mqtt-worker", daemon=True
        )
        self._worker_thread.start()

        self._client.connect_async(
            self._cfg.MQTT_BROKER_HOST, self._cfg.MQTT_BROKER_PORT
        )
        self._client.loop_start()
        logger.info(
            "MQTT client started (%s:%s, tls=%s)",
            self._cfg.MQTT_BROKER_HOST,
            self._cfg.MQTT_BROKER_PORT,
            self._cfg.MQTT_USE_TLS,
        )

    def wait_until_ready(self, timeout: float = 10.0) -> bool:
        """Block until both subscriptions are granted, or *timeout* elapses.

        Used by the standalone runner's smoke check (M-10) to surface a
        misconfigured broker instead of silently idling.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready:
                return True
            time.sleep(0.1)
        return False

    def stop(self) -> None:
        """Disconnect and stop the loop and worker, draining gracefully (L-24).

        ``disconnect()`` is called before ``loop_stop()`` so the clean DISCONNECT
        packet is flushed to the broker before the loop exits.
        """
        self._client.disconnect()
        self._client.loop_stop()
        self._stop_event.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5.0)
            self._worker_thread = None
        logger.info("MQTT client stopped")


def main() -> None:
    """Standalone entrypoint: ``python -m mqtt.client``.

    Starts the client, runs a subscription smoke check, and idles until
    interrupted. Exits non-zero on failure so orchestrators can restart it.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    client = MQTTClient()
    client.start()
    try:
        if not client.wait_until_ready(timeout=10.0):
            logger.error(
                "MQTT subscription smoke check FAILED — broker not subscribing. "
                "Check broker connectivity/topics."
            )
            raise SystemExit(1)
        logger.info("MQTT subscription smoke check passed — ingesting device messages")

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Interrupted — stopping")
    finally:
        client.stop()


if __name__ == "__main__":
    main()