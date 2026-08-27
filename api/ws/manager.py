"""
WebSocket connection manager — tracks connected clients and broadcasts to them.

The MQTT ``air/alerts`` bridge (Phase 9) runs on paho's worker thread, which is
**not** the Quart asyncio loop. ``broadcast()`` is therefore safe to call from
any thread: it marshals the send onto the captured loop with
``asyncio.run_coroutine_threadsafe``. Sockets are only ever mutated from the
loop thread (``connect``/``disconnect`` are async and run there), while
``broadcast`` *schedules* the actual send — so a lock only needs to guard the
connection set itself, and the send sweep drops dead sockets it finds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

logger = logging.getLogger("empyrean.ws")


class ConnectionManager:
    """Thread-safe registry of open WebSocket clients."""

    def __init__(self) -> None:
        self._connections: set[Any] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    async def connect(self, websocket: Any) -> None:
        """Register a client, capturing the running event loop.

        The loop is re-captured when the previously recorded one is no longer
        running (e.g. an app restart, or a test that spins up a fresh
        ``asyncio`` loop). This keeps ``broadcast`` scheduled onto a live loop;
        without it a stale, closed loop would silently drop every broadcast.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                self._loop = loop
            self._connections.add(websocket)

    async def disconnect(self, websocket: Any) -> None:
        """Remove a client."""
        with self._lock:
            self._connections.discard(websocket)

    @property
    def connected_count(self) -> int:
        with self._lock:
            return len(self._connections)

    def broadcast(self, message: Any) -> tuple[bool, int]:
        """Send ``message`` (JSON-serialized) to every connected client.

        Thread-safe: callable from the MQTT worker thread. Returns a tuple
        ``(success, count)`` where ``success`` indicates whether broadcast
        started successfully and ``count`` is the number of clients targeted.
        Silently drops serialization errors and dead sockets (#14).
        """
        with self._lock:
            connections = list(self._connections)
            loop = self._loop
        if not connections or loop is None:
            return (False, 0)

        try:
            payload = json.dumps(message)
        except (TypeError, ValueError):
            logger.warning("Cannot JSON-serialize broadcast message — dropped")
            return (False, len(connections))

        try:
            asyncio.run_coroutine_threadsafe(
                self._send_all(payload, connections), loop
            )
            return (True, len(connections))
        except RuntimeError as e:
            # Loop is not running / already closed — nothing to deliver.
            # M74: mark the captured loop stale so the next ``connect()``
            # recaptures the live loop instead of every later broadcast
            # silently dropping against the dead one (the drop used to be a
            # debug log and nothing more).
            logger.debug("WS loop not running — dropping broadcast: %s", e)
            with self._lock:
                if self._loop is loop:
                    self._loop = None
            return (False, len(connections))

    async def _send_all(self, payload: str, connections: list[Any]) -> None:
        """Deliver ``payload`` to all sockets concurrently (M87).

        The old sequential loop let one slow client stall every other socket's
        delivery. ``gather(..., return_exceptions=True)`` sends in parallel and
        reports per-socket failures without cancelling the healthy sends; the
        futures returned by ``run_coroutine_threadsafe`` are observed here
        (exception results are inspected, never discarded silently).
        """
        results = await asyncio.gather(
            *(ws.send(payload) for ws in connections), return_exceptions=True
        )
        dead = [
            ws for ws, result in zip(connections, results) if isinstance(result, BaseException)
        ]
        if dead:
            with self._lock:
                for ws in dead:
                    try:
                        self._connections.discard(ws)
                    except TypeError:
                        # Non-hashable slipped into the set (e.g. a mis-resolved
                        # proxy) — beat it out by identity instead of dropping.
                        for candidate in list(self._connections):
                            if candidate is ws:
                                self._connections.discard(candidate)
                                break
            logger.info("Dropped %d dead WebSocket client(s)", len(dead))


manager = ConnectionManager()