"""
Device config publisher — pushes remote configuration out to a node.

Publishes to ``air/node/{node_id}/config`` at QoS 1. The ``node_id`` is
validated against a strict pattern to prevent topic injection (a crafted id
must not be able to inject ``/`` path segments or ``#``/``+`` wildcards).
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("empyrean.mqtt")

# Only safe topic-path characters — no slashes, no wildcards.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")

_TOPIC_TEMPLATE = "air/node/{node_id}/config"
_QOS = 1


def config_topic(node_id: str) -> str:
    """Return the validated ``air/node/{node_id}/config`` topic (L22).

    Single helper pairing the strict id check with the topic template, so a
    crafted id can never escape the intended topic path and the two can
    never drift apart.
    """
    if not _NODE_ID_RE.fullmatch(node_id):
        raise ValueError(
            f"node_id {node_id!r} is not a valid device id "
            "(only A-Za-z0-9, '_', '-', 1-50 chars)"
        )
    return _TOPIC_TEMPLATE.format(node_id=node_id)


def publish_config(
    client,
    node_id: str,
    *,
    interval_s: int = 30,
    fuzzy_enabled: bool = True,
    enabled: bool = True,
) -> None:
    """Publish ``{interval_s, fuzzy_enabled, enabled}`` to a node's config topic.

    Raises ``ValueError`` if ``node_id`` contains characters that could
    escape the intended topic path (topic injection guard), or if
    ``interval_s`` is outside the device-sanctioned range (L-22). Payload
    errors from the broker are logged, never raised.

    M26: ``enabled=False`` tells the device to stop publishing (sent when a
    node is deactivated). Firmware predating the field ignores it — such
    devices keep their last cadence until reconfigured or rebooted.
    """
    topic = config_topic(node_id)
    if not 1 <= interval_s <= 86400:
        raise ValueError(
            f"interval_s {interval_s!r} is out of range (must be 1..86400)"
        )

    payload = json.dumps(
        {"interval_s": interval_s, "fuzzy_enabled": fuzzy_enabled, "enabled": enabled}
    )
    logger.debug("Publishing config %s -> %s", payload, topic)
    info = client.publish(topic, payload, qos=_QOS)
    if info.rc != 0:
        logger.warning(
            "Failed to publish config to %s (rc=%s): %s",
            topic,
            info.rc,
            info,
        )