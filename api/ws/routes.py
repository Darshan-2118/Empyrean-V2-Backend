"""
WebSocket endpoint — real-time alert broadcasts.

``/ws/alerts`` is a broadcast-only socket: the server pushes MQTT ``air/alerts``
messages to every connected client (via ``api.ws.manager``) and never echoes
client frames. Auth is JWT, validated **before** ``accept()`` — an
unauthenticated handshake is closed rather than accepted.

Token resolution order:
1. ``Authorization: Bearer <access_token>`` header (non-browser clients), then
2. ``?token=<access_token>`` query param (browser WebSockets cannot set headers).

Origin validation:
- WebSocket connections are validated against allowed origins to prevent
  cross-site WebSocket hijacking
- Uses the same origin list as CORS configuration for consistency

The socket is only live once the MQTT broker publishes to ``air/alerts``; with
no broker the client connects but receives nothing until a broadcast arrives.
"""

from __future__ import annotations

import logging

from quart import Blueprint, request, websocket

from api.jwt import decode_access_token
from api.ws.manager import manager
from config import get_config
from models import User
from models.base import AsyncSessionLocal

logger = logging.getLogger("empyrean.ws")

ws_bp = Blueprint("ws", __name__)

_MAX_WS_BODY = 4096  # cap on frames this push-only socket accepts from a client


def _access_token() -> str | None:
    """Resolve the JWT from the Authorization header or ``?token`` query param."""
    auth = websocket.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.partition(" ")[2].strip()
    return websocket.args.get("token") or None


def _is_origin_allowed(origin: str | None) -> bool:
    """Check if the origin is in the allowed list.

    Args:
        origin: The Origin header value from the WebSocket handshake

    Returns:
        True if origin is allowed or if no origin is provided (same-origin),
        False otherwise
    """
    # Same-origin requests (no Origin header) are allowed
    if origin is None:
        return True

    config = get_config()
    allowed_origins = config.cors_origins_list

    # Exact match against allowed origins
    return origin in allowed_origins


@ws_bp.websocket("/alerts")
async def alerts_ws() -> None:
    """Authenticate, accept, then stream alert broadcasts until disconnect."""
    # Origin validation: prevent cross-site WebSocket hijacking by checking
    # the Origin header against the configured allowlist. Same-origin requests
    # (no Origin header) are allowed. Rejected connections are closed before
    # any token is processed to avoid leaking auth state.
    origin = websocket.headers.get("Origin")
    if not _is_origin_allowed(origin):
        logger.warning(
            "WebSocket /ws/alerts rejected: origin %r not in allowlist",
            origin,
        )
        await websocket.close(code=4403)  # 4403 = forbidden origin
        return

    token = _access_token()
    try:
        if token is None:
            raise ValueError("missing token")
        payload = decode_access_token(token)
        # Mirror the REST auth path (api.jwt._authenticate_user): reject a
        # soft-deleted or removed account rather than feeding its alerts.
        async with AsyncSessionLocal() as session:
            user = await session.get(User, payload["sub"])
        if user is None or not user.is_active:
            raise ValueError("user missing or inactive")
    except Exception as exc:  # noqa: BLE001 — any auth failure rejects the handshake
        # Reject the handshake: never accept an unauthenticated socket.
        logger.warning(
            "WebSocket /ws/alerts auth rejected: %s: %s", type(exc).__name__, exc
        )
        await websocket.close(code=4401)
        return

    await websocket.accept()
    # Resolve the concrete Websocket before storing it. ``manager`` broadcasts
    # from the MQTT worker thread via ``run_coroutine_threadsafe`` — a context
    # switch outside the request scope would leave the ``websocket`` LocalProxy
    # unbound and make every later ``send`` raise "Not within a websocket
    # context". The proxy is only guaranteed to resolve while the handler runs.
    sock = websocket._get_current_object()
    await manager.connect(sock)
    logger.info("WebSocket client connected to /ws/alerts")

    try:
        # Push-only: drain any client frames; exit when the peer closes.
        # Enforce _MAX_WS_BODY to prevent memory exhaustion from large frames.
        while True:
            try:
                frame = await websocket.receive()
            except Exception:
                break
            if frame is None:
                break
            # Validate frame size
            if isinstance(frame, (str, bytes)) and len(frame) > _MAX_WS_BODY:
                logger.warning(
                    "WebSocket frame size %d exceeds limit %d — closing connection",
                    len(frame), _MAX_WS_BODY
                )
                await websocket.close(code=1009)  # 1009 = message too big
                return
    finally:
        await manager.disconnect(sock)
        logger.info("WebSocket client disconnected from /ws/alerts")