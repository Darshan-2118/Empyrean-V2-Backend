"""
MQTT client registry — the single place API routes get the *running* broker client.

``app.py`` registers the live :class:`mqtt.client.MQTTClient` on startup
(``set_client``) and clears it on shutdown (``set_client(None)``). Routes that
need to push device config (Phase 8 ``PATCH /nodes/:node_id``) call
``get_client()`` and degrade to ``config_pushed: false`` when it is ``None``
(broker disabled / not yet started / torn down). Fail-open, matching the rest
of the MQTT touchpoints.
"""

from __future__ import annotations

_client = None


def set_client(client) -> None:
    """Record the running MQTT client (or clear it with ``None``)."""
    global _client
    _client = client


def get_client():
    """Return the running MQTT client, or ``None`` if none is registered."""
    return _client
