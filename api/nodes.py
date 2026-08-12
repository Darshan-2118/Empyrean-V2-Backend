"""Nodes blueprint — list, register, and re-configure sensor nodes.

Routes:
* ``GET /nodes``            — all registered nodes (Redis-cached ``nodes:all``, TTL 300s).
* ``POST /nodes``           — self-service registration of a new node (any JWT).
* ``PATCH /nodes/:node_id`` — admin-only; update metadata / reading interval /
  active status; pushes the reading interval to the device via MQTT (fail-open).

All routes are rate-limited. Errors are RFC 7807 problem JSON.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from quart import Blueprint, jsonify

from api.cache import cache_delete, cache_get_json, cache_set_json
from api.jwt import _problem_json, admin_required, jwt_required
from api.rate_limit import rate_limit
from api.schemas import NodeResponse, RegisterNodeRequest, UpdateNodeRequest
from api.validation import validate_body, validated_body
from models import Node
from models.base import AsyncSessionLocal
from mqtt.registry import get_client

logger = logging.getLogger("empyrean.nodes")

nodes_bp = Blueprint("nodes", __name__)

# Global read-through cache for the node list (contract docs/database.md).
_NODES_CACHE_KEY = "nodes:all"
_NODES_CACHE_TTL = 300
_READINGS_LATEST_KEY = "readings:latest"


def _serialise(node: Node) -> dict:
    """Convert a ``Node`` ORM row into a ``NodeResponse`` dict."""
    return NodeResponse(
        node_id=node.node_id,
        name=node.name,
        location_name=node.location_name,
        lat=node.lat,
        lon=node.lon,
        firmware_version=node.firmware_version,
        reading_interval=node.reading_interval,
        is_active=node.is_active,
        registered_at=node.registered_at,
        last_seen=node.last_seen,
    ).model_dump()


def _push_config(node_id: str, interval_s: int) -> bool:
    """Best-effort push of a reading interval to a device (fail-open).

    Returns ``True`` only if a client is registered and the publish did not
    raise. Never raises to the caller. When no client is available (broker
    disabled / not started) it logs and returns ``False``.
    """
    client = get_client()
    if client is None:
        logger.warning("No MQTT client available — skipping config push for %s", node_id)
        return False
    try:
        from mqtt.config import publish_config  # import here to keep route import light
        publish_config(client, node_id, interval_s=interval_s)
    except Exception:
        logger.exception("Config push failed for node %s", node_id)
        return False
    return True


@nodes_bp.route("", methods=["GET"])
@rate_limit()
@jwt_required
async def list_nodes():
    """All registered nodes with metadata (cached under ``nodes:all``, TTL 300s)."""
    cached = await cache_get_json(_NODES_CACHE_KEY)
    if cached is not None:
        return jsonify({"nodes": cached}), 200

    stmt = select(Node).order_by(Node.node_id)
    async with AsyncSessionLocal() as session:
        rows = list((await session.execute(stmt)).scalars().all())

    payload = [_serialise(n) for n in rows]
    await cache_set_json(_NODES_CACHE_KEY, payload, _NODES_CACHE_TTL)
    return jsonify({"nodes": payload}), 200


@nodes_bp.route("", methods=["POST"])
@rate_limit()
@jwt_required
@validate_body(RegisterNodeRequest)
async def register_node():
    """Register a new sensor node (self-service; any authenticated user)."""
    data = validated_body()

    node = Node(
        node_id=data.node_id,
        name=data.name,
        location_name=data.location_name,
        lat=data.lat,
        lon=data.lon,
        firmware_version=data.firmware_version,
        reading_interval=data.reading_interval,
        is_active=True,
    )
    async with AsyncSessionLocal() as session:
        try:
            session.add(node)
            await session.commit()
            await session.refresh(node)
        except IntegrityError:
            await session.rollback()
            return _problem_json(409, "Conflict", "A node with this id is already registered")

    await cache_delete(_NODES_CACHE_KEY)
    return jsonify(_serialise(node)), 201


@nodes_bp.route("/<node_id>", methods=["PATCH"])
@rate_limit()
@admin_required
@validate_body(UpdateNodeRequest)
async def update_node(node_id: str):
    """Update metadata / config (admin only); push reading interval via MQTT."""
    data = validated_body()

    async with AsyncSessionLocal() as session:
        node = await session.get(Node, node_id)
        if node is None:
            return _problem_json(404, "Not Found", "Node not found")

        if data.name is not None:
            node.name = data.name
        if data.location_name is not None:
            node.location_name = data.location_name
        if data.lat is not None:
            node.lat = data.lat
        if data.lon is not None:
            node.lon = data.lon
        if data.firmware_version is not None:
            node.firmware_version = data.firmware_version
        if data.reading_interval is not None:
            node.reading_interval = data.reading_interval
        if data.is_active is not None:
            node.is_active = data.is_active

        await session.commit()
        await session.refresh(node)
        serialised = _serialise(node)

    # Push the reading interval to the device (fail-open).
    pushed = False
    if data.reading_interval is not None:
        pushed = _push_config(node_id, data.reading_interval)

    await cache_delete(_NODES_CACHE_KEY)
    if data.is_active is not None:
        # is_active feeds the active-only readings:latest query — drop that cache.
        await cache_delete(_READINGS_LATEST_KEY)

    serialised["config_pushed"] = pushed
    return jsonify(serialised), 200