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
import socket
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
_ALERTS_TOPIC = "air/alerts"
_QOS = 1

# Fixed client id + persistent session (clean_session=False, L-20): offline
# delivery of queued QoS1 device messages is honored, consistent with the
# at-least-once contract and the M-8 bounded-queue/retry durability path.
# H36: this legacy constant is only a *prefix* now — the runtime id appends
# the hostname (or uses MQTT_CLIENT_ID) so two API hosts / a dev instance
# against the prod broker never fight over one broker session.
_CLIENT_ID = "empyrean-backend"

# Worker-queue backpressure bounds (I-35): configurable via config/__init__.py
# The queue is bounded to bound memory; production should pair this with the Celery acks_late path.
_QUEUE_MAX = get_config().MQTT_QUEUE_MAX
_ENQUEUE_MAX_ATTEMPTS = get_config().MQTT_ENQUEUE_MAX_ATTEMPTS
_ENQUEUE_TIMEOUT = get_config().MQTT_ENQUEUE_TIMEOUT

# Dispatch retry bounds for a transient Celery/Redis outage (I-35): configurable
# via config/__init__.py.
_DISPATCH_MAX_ATTEMPTS = 3
_DISPATCH_RETRY_DELAY = 0.5  # seconds

# Truncation length for logging raw payloads (L-21).
_LOG_TRUNCATE = 200

# Track dropped readings and queue overflow events (#04 — observability)
# Incremented when dispatch fails after retries or queue overflows
_dropped_readings_count = 0
_dropped_readings_lock = threading.Lock()
_queue_overflow_count = 0
_queue_overflow_lock = threading.Lock()


def _truncated_repr(value: object) -> str:
    """``repr`` of *value* capped at ``_LOG_TRUNCATE`` chars for safe logging."""
    text = repr(value)
    if len(text) > _LOG_TRUNCATE:
        return text[:_LOG_TRUNCATE] + f"...<{len(text) - _LOG_TRUNCATE} more>"
    return text


def get_dropped_readings_count() -> int:
    """Return the count of MQTT readings dropped since app startup (#04).
    
    Used by /admin/health to surface data loss events. A non-zero count
    indicates readings failed to enqueue after bounded retry.
    """
    with _dropped_readings_lock:
        return _dropped_readings_count


def get_queue_overflow_count() -> int:
    """Return the count of queue overflow events since app startup (#04).
    
    Used by /admin/health to surface when the MQTT worker queue exceeded
    capacity and dropped messages.
    """
    with _queue_overflow_lock:
        return _queue_overflow_count


def _increment_dropped_readings() -> None:
    """Increment dropped readings counter (#04)."""
    global _dropped_readings_count
    with _dropped_readings_lock:
        _dropped_readings_count += 1


def _increment_queue_overflow() -> None:
    """Increment queue overflow counter (#04)."""
    global _queue_overflow_count
    with _queue_overflow_lock:
        _queue_overflow_count += 1


# M39: per-node inbound rate bound. A flooding (or broken) node used to fill
# the worker queue with payloads that each cost json.loads + pydantic
# validation with no bound. Devices report on a ~30 s cadence, so anything
# above this per-node frequency is excess and is dropped before it reaches
# the worker queue. Keyed by topic-validated node ids — regex-validated only,
# never checked against the fleet table, hence the M102 cap/eviction below.
_NODE_MSG_MIN_INTERVAL_S = 0.5
# M102: bound _node_last_seen — a device with valid broker credentials can
# publish unique random node ids at wire rate, and entries were never evicted
# (unbounded memory growth). When the dict exceeds the cap on insertion,
# prune oldest-first (by last-seen time) back to the prune target so the hot
# path only pays the sort occasionally, not on every insert over cap.
_NODE_LAST_SEEN_MAX = 10_000
_NODE_LAST_SEEN_PRUNE_TO = 9_000
_node_last_seen: dict[str, float] = {}
_node_last_seen_lock = threading.Lock()


def _node_rate_limited(node_id: str) -> bool:
    """True when *node_id* sent a message less than the min interval ago (M39)."""
    now = time.monotonic()
    with _node_last_seen_lock:
        last = _node_last_seen.get(node_id)
        if last is not None and now - last < _NODE_MSG_MIN_INTERVAL_S:
            return True
        _node_last_seen[node_id] = now
        if len(_node_last_seen) > _NODE_LAST_SEEN_MAX:
            # M102: evict oldest-first back under the cap.
            oldest_first = sorted(_node_last_seen, key=_node_last_seen.get)
            for key in oldest_first[: len(_node_last_seen) - _NODE_LAST_SEEN_PRUNE_TO]:
                del _node_last_seen[key]
        return False


# M101: backlog-replay exemption for the M39 limiter. clean_session=False lets
# the broker keep our offline QoS1 queue, and after any restart Mosquitto
# delivers that backlog as a fast burst the per-node limiter would mostly drop.
# A reading whose device timestamp is older than this threshold is replayed
# backlog and bypasses the limiter; fresh (current-timestamp) readings stay
# rate-limited as before.
_BACKLOG_FRESHNESS_S = 60.0


def _device_time_is_stale(raw: str) -> bool:
    """True when the payload's device ``time`` is backlog, not fresh (M101).

    Missing/unparseable/non-string timestamps are treated as fresh so the
    limiter keeps applying — only an explicitly old device timestamp exempts
    a message.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    value = data.get("time")
    if not isinstance(value, str):
        return False
    try:
        device_time = datetime.fromisoformat(value)
    except ValueError:
        return False
    if device_time.tzinfo is None:
        device_time = device_time.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - device_time).total_seconds()
    return age >= _BACKLOG_FRESHNESS_S


# L70: inbound payload size cap, enforced before decode/enqueue. Mosquitto's
# default message_size_limit is 256 MB and the worker queue holds up to
# _QUEUE_MAX raw strings, so an uncapped decode lets a credentialed device
# OOM the process.
_MAX_PAYLOAD_BYTES = 64 * 1024

# L69: minimum spacing between worker-tick re-subscribe attempts after a
# failed/denied subscribe, so a persistently-denying broker isn't hammered.
_SUBSCRIBE_RETRY_INTERVAL_S = 5.0


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
    
    On final failure after all retries, increments _dropped_readings_count (#04).
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
                    "Giving up dispatching reading for node %r after %d attempts — reading DROPPED",
                    node_id,
                    attempt,
                )
                _increment_dropped_readings()
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


def _handle_alert(raw: str) -> None:
    """Forward an ``air/alerts`` message to WebSocket clients (thread-safe).

    Broadcasts start asynchronously; success/failure is logged but not
    propagated up to avoid compounding errors (#14).
    """
    data = _json_loads(raw, "alert")
    if data is None:
        return
    from api.ws.manager import manager  # import here to avoid an import cycle

    res = manager.broadcast(data)
    if isinstance(res, tuple):
        success, count = res
        if not success:
            logger.warning("Broadcast failed to target %d client(s)", count)


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
        self._ready = False  # True once both reading and status subscriptions are granted
        # M36: readiness is tracked by *which* required topics have been
        # granted, not by a SUBACK counter — a counter let an unusual grant
        # order (e.g. reading + alerts first) mark the client ready without
        # the status topic ever being subscribed.
        self._granted_topics: set[str] = set()
        self._tls_configured = False  # True only after tls_set() succeeds (H-4)
        # L69: a failed/denied subscribe leaves the client connected but never
        # ready; paho does not retry it, so the worker tick re-subscribes.
        self._subscribe_failed = False
        self._next_subscribe_retry = 0.0

        # H36: unique-per-host session id. An explicit MQTT_CLIENT_ID wins;
        # otherwise derive from the hostname so a second backend host (or a
        # dev instance pointed at the same broker) gets its own session rather
        # than triggering an endless CONNECT-takeover loop with this one.
        # getattr (not attribute access) so test stubs and minimal cfg objects
        # without this newer field keep working.
        resolved_client_id = (
            getattr(self._cfg, "MQTT_CLIENT_ID", "")
            or f"{_CLIENT_ID}-{socket.gethostname().lower()}"
        )

        self._client = mqtt.Client(
            client_id=resolved_client_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe
        self._client.on_disconnect = self._on_disconnect
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
        subscribe_failed = False
        for topic in (_READING_TOPIC, _STATUS_TOPIC, _ALERTS_TOPIC):
            result, mid = client.subscribe(topic, qos=_QOS)
            if result != mqtt.MQTT_ERR_SUCCESS:
                logger.error("Failed to subscribe to %s (rc=%s)", topic, result)
                subscribe_failed = True
            else:
                self._pending_subs[mid] = topic
        # L69: paho never retries a failed subscribe() and the client would
        # stay connected with _ready False forever. disconnect() is not an
        # option either — paho 1.6's loop_forever exits its thread for good
        # when a callback disconnects — so flag it for the worker-tick retry.
        self._subscribe_failed = subscribe_failed

    def _on_subscribe(self, client, userdata, mid, granted_qos) -> None:
        """Verify the granted QoS on SUBACK and surface denials (L-23).

        ``subscribe()`` returning SUCCESS only means the SUBSCRIBE packet was
        sent. A broker can deny it in the SUBACK; inspecting the granted QoS is
        the authoritative check.

        Only sets `_ready=True` after BOTH required subscriptions
        (reading + status) are successfully granted — tracked by topic name,
        never by SUBACK arrival order (M36).
        """
        topic = self._pending_subs.pop(mid, None)
        granted = granted_qos[0] if granted_qos else None
        if topic is None:
            logger.debug("SUBACK for unknown subscribe mid=%s (reconnect race)", mid)
            return
        if granted is None or granted >= 0x80:
            logger.error(
                "Subscription to %s denied by broker (granted QoS %s)", topic, granted
            )
            # L69: a denied subscription also leaves the client never-ready;
            # retry it from the worker tick alongside failed subscribe() calls.
            self._subscribe_failed = True
            return

        self._granted_topics.add(topic)
        if {_READING_TOPIC, _STATUS_TOPIC, _ALERTS_TOPIC} <= self._granted_topics:
            self._subscribe_failed = False  # L69: everything is subscribed now

        if {_READING_TOPIC, _STATUS_TOPIC} <= self._granted_topics:
            if not self._ready:
                self._ready = True
                logger.info(
                    "MQTT client ready — both required topics subscribed: "
                    "%s, %s @ granted QoS %s", _READING_TOPIC, _STATUS_TOPIC, granted
                )
        elif topic in (_READING_TOPIC, _STATUS_TOPIC):
            logger.info(
                "Subscribed to %s @ granted QoS %s (%d/2 required)",
                topic, granted, len(self._granted_topics & {_READING_TOPIC, _STATUS_TOPIC}),
            )
        else:
            # Alerts subscription is optional from a readiness perspective.
            logger.info(
                "Alerts subscription granted @ QoS %s (ready check uses reading/status)",
                granted
            )

    def _on_disconnect(self, client, userdata, rc) -> None:
        """Reset subscription/readiness state when the broker connection drops.

        M88: ``_ready`` used to survive disconnects, so one good session made
        readiness permanent across broker loss. L23: ``_pending_subs`` is
        cleared too — mids from the lost session can never match SUBACKs after
        a reconnect (``_on_connect`` re-subscribes and re-registers them).
        """
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect (rc=%s) — readiness reset", rc)
        self._ready = False
        self._granted_topics.clear()
        self._pending_subs.clear()
        # L69: the next _on_connect re-subscribes and re-sets the flag as needed.
        self._subscribe_failed = False

    def _maybe_retry_subscribes(self) -> None:
        """Re-issue failed/denied subscriptions on a throttle (L69).

        Called from the worker tick. paho never retries a failed subscribe()
        from ``_on_connect`` and a disconnect from the callback would exit
        the loop thread for good, so failed subscribes are flagged there and
        re-issued here while the client is connected.
        """
        if not self._subscribe_failed:
            return
        now = time.monotonic()
        if now < self._next_subscribe_retry:
            return
        self._next_subscribe_retry = now + _SUBSCRIBE_RETRY_INTERVAL_S
        if not self._client.is_connected():
            return
        for topic in (_READING_TOPIC, _STATUS_TOPIC, _ALERTS_TOPIC):
            if topic in self._granted_topics:
                continue
            result, mid = self._client.subscribe(topic, qos=_QOS)
            if result != mqtt.MQTT_ERR_SUCCESS:
                logger.error("Retry subscribe to %s failed (rc=%s)", topic, result)
            else:
                self._pending_subs[mid] = topic

    def _on_message(self, client, userdata, msg) -> None:
        """Decode + route on the paho loop; heavier work goes to the worker."""
        try:
            # L70: drop oversized payloads before any decode/enqueue.
            if len(msg.payload) > _MAX_PAYLOAD_BYTES:
                logger.warning(
                    "Dropping oversized payload on %r (%d bytes > %d) (L70)",
                    msg.topic, len(msg.payload), _MAX_PAYLOAD_BYTES,
                )
                return
            raw = _resolve_payload(msg.payload)
            if raw is None:
                return
            if msg.topic == _ALERTS_TOPIC:
                self._enqueue(_ALERTS_TOPIC, "alert", raw)
                return
            match = _TOPIC_RE.fullmatch(msg.topic)
            if not match:
                logger.debug("Dropping unknown topic %r", msg.topic)
                return
            
            node_id = match.group("node_id")
            # Re-validate node_id against strict pattern to prevent injection (#7)
            # Topic regex allows any character, but node_id must be safe
            from mqtt.config import _NODE_ID_RE as VALID_NODE_ID_PATTERN
            if not VALID_NODE_ID_PATTERN.fullmatch(node_id):
                logger.warning(
                    "Rejecting message with invalid node_id %r from topic %r",
                    node_id,
                    msg.topic
                )
                return

            # M39: bound per-node inbound cadence before the worker queue so
            # a flooding node can't force unbounded parse/validate cost.
            # M101: replayed backlog (device timestamp older than the
            # freshness threshold) bypasses the limiter so the broker's
            # offline-queued QoS1 burst isn't dropped on reconnect.
            if _node_rate_limited(node_id) and not _device_time_is_stale(raw):
                logger.debug(
                    "Rate-limiting inbound %s for node %r (M39)", match.group("kind"), node_id
                )
                return

            self._enqueue(node_id, match.group("kind"), raw)
        except Exception:
            logger.exception("Unhandled error in on_message for %r", msg.topic)

    def _enqueue(self, node_id: str, kind: str, raw: str) -> None:
        """Push a message to the worker queue with bounded backpressure.

        A transiently-full queue is retried so a slow worker doesn't force drops,
        but a persistently-full queue (worker can't keep up) drops with a
        warning rather than blocking the paho loop (M-9).

        M37: a drop here also increments the queue-overflow counter — it used
        to be logged-only, so ``get_queue_overflow_count()`` stayed 0 forever
        and /admin/health could never surface backpressure data loss.
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
        _increment_queue_overflow()

    def _run_worker(self) -> None:
        """Consume the dispatch queue on a dedicated thread (M-9)."""
        while not self._stop_event.is_set():
            self._maybe_retry_subscribes()  # L69: throttled subscribe retry
            try:
                node_id, kind, raw = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if kind == "reading":
                    _handle_reading(node_id, raw)
                elif kind == "alert":
                    _handle_alert(raw)
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

        # L76: a duplicate start() while the worker is alive used to spawn a
        # second worker thread (and replace the stop event the first one was
        # watching). Ignore it instead of leaking threads.
        if self._worker_thread is not None and self._worker_thread.is_alive():
            logger.warning("MQTT client already started — ignoring duplicate start()")
            return

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
        """Disconnect and stop the loop and worker, draining gracefully (I-42).

        Drains the worker queue before exiting to ensure no pending readings are lost.
        ``disconnect()`` is called before ``loop_stop()`` so the clean DISCONNECT
        packet is flushed to the broker before the loop exits.
        """
        logger.info("Stopping MQTT client — draining queue before disconnect")

        # Stop accepting new messages
        self._stop_event.set()

        # Drain pending messages before disconnecting
        drained_count = 0
        max_drain_attempts = _QUEUE_MAX * 2  # Generous timeout to drain all
        drain_deadline = time.monotonic() + 5.0  # 5 second cleanup window

        while drained_count < max_drain_attempts:
            try:
                node_id, kind, raw = self._queue.get(timeout=0.1)
                handled = False

                if kind == "reading":
                    _handle_reading(node_id, raw)
                    handled = True
                elif kind == "alert":
                    _handle_alert(raw)
                    handled = True
                else:
                    # Handle status (not needed for drain, just drain)
                    pass

                if handled:
                    drained_count += 1

            except queue.Empty:
                # Queue empty, ready to exit
                break
            except Exception:
                # Log errors during drain but continue
                logger.exception("Error draining message during shutdown")
                continue

            if time.monotonic() > drain_deadline:
                logger.warning(
                    "MQTT queue drain timeout after %ds, %d/%d messages processed",
                    5.0,
                    drained_count,
                    _QUEUE_MAX
                )
                break

        if drained_count > 0:
            logger.info("Drained %d pending messages before shutdown", drained_count)

        logger.info("Disconnecting MQTT broker...")
        self._client.disconnect()
        logger.info("Stopping MQTT event loop...")

        self._client.loop_stop()
        logger.info("MQTT client stopped")

    def is_connected(self) -> bool:
        """True when the underlying paho client reports a live broker connection.

        Read by the admin health check (``GET /admin/health``); the paho loop
        updates this state on CONNACK and on disconnect, so it is the current
        connection status rather than "has ever connected".
        """
        return bool(self._client.is_connected())

    def publish(self, topic: str, payload=None, qos: int = 0, retain: bool = False):
        """Delegate a publish to the underlying paho client (H34).

        ``mqtt.registry`` hands **this wrapper** to API code; without this
        delegation, ``mqtt.config.publish_config(client, …)`` raised
        ``AttributeError`` on every call and each PATCH ``/nodes/:node_id``
        config push silently failed (``config_pushed: false`` forever).
        Thread-safe: paho's ``Client.publish`` may be called from any thread.
        """
        return self._client.publish(topic, payload=payload, qos=qos, retain=retain)


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