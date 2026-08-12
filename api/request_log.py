"""
App-wide HTTP request logging (API side).

Installs Quart ``before_request`` / ``after_request`` hooks that emit exactly
one INFO record per HTTP request on the dedicated ``empyrean.request`` logger,
so it can be tuned or filtered independently of the app logger. The line
carries the four fields: ``method``, ``path``, ``status``, ``duration_ms``.

Security rules (this repo is strict):
* No request bodies, no auth tokens, and no credentials from query strings are
  ever logged — ``path`` is ``request.path`` *without* the query string, so a
  ``?token=...`` in a request never appears in the line.
* The client address is never taken from a client-supplied ``X-Forwarded-For``
  header (same rule as rate limiting, H-5); the line omits IPs entirely rather
  than risk echoing an untrusted value.

Duration is measured with ``time.perf_counter`` from ``before_request`` to
``after_request`` — i.e. the time the app took to *prepare* the response. For a
streaming response (``GET /export``) the body streams after the response object
is produced, so the duration covers validation + setup, not the stream send —
the CSV stream itself is intentionally untouched.

WebSocket requests are not HTTP requests: Quart runs neither ``before_request``
nor ``after_request`` for a websocket handshake, so this logger never sees the
WS path or its ``?token=`` query param.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from quart import Quart, request

logger = logging.getLogger("empyrean.request")


def register_request_logging(app: Quart) -> None:
    """Install before/after_request hooks that log one line per HTTP request."""

    @app.before_request
    async def _record_request_start() -> None:
        # perf_counter is monotonic (immune to clock changes), so the
        # after_request hook can measure wall time reliably. Stored on the
        # request object, not a module global, so concurrent requests never
        # cross-start each other.
        request._empyrean_request_start = time.perf_counter()

    @app.after_request
    async def _log_request(response: Any) -> Any:
        start = getattr(request, "_empyrean_request_start", None)
        duration_ms = (
            (time.perf_counter() - start) * 1000.0 if start is not None else 0.0
        )
        logger.info(
            "method=%s path=%s status=%s duration_ms=%.2f",
            request.method,
            request.path,
            response.status_code,
            duration_ms,
        )
        return response
