"""Unit tests for the Phase 8 Node schemas (``api/schemas.py``).

* A ``node_id`` must not be able to escape the MQTT topic path — any id
  containing a path separator or wildcard, or empty, is rejected by
  ``RegisterNodeRequest``.
* ``NodeResponse`` must serialise its datetimes as ISO-8601 with a trailing
  ``Z`` (docs contract), and a ``None`` ``last_seen`` must stay ``None``.
"""

from __future__ import annotations

import pydantic
import pytest

from api.schemas import NodeResponse, RegisterNodeRequest


def test_register_node_schema_rejects_topic_injection():
    """A node_id that could escape the MQTT topic path must be invalid."""
    for bad in ("a/b", "a#", "a+", ""):
        with pytest.raises(pydantic.ValidationError):
            RegisterNodeRequest(node_id=bad)
    ok = RegisterNodeRequest(node_id="ESP32-01", reading_interval=60)
    assert ok.reading_interval == 60


def test_node_response_serialises_datetimes_as_iso_z():
    from datetime import datetime, timezone

    node = NodeResponse(
        node_id="n",
        reading_interval=30,
        is_active=True,
        registered_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        last_seen=None,
    )
    dumped = node.model_dump()
    assert dumped["registered_at"].endswith("Z")
    assert dumped["last_seen"] is None
