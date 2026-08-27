"""
Pydantic request-body validation middleware (API side).

Decorator ``@validate_body(Schema)`` applied to JSON-body endpoints. It awaits
``request.get_json(silent=True)``, runs ``Schema.model_validate(...)``
(pydantic v2), and stores the validated model on the request where the route
reads it back via :func:`validated_body`. On validation failure it returns an
RFC 7807 ``422 Unprocessable Entity`` (``problem_json`` with the pydantic
error text) — never a 500, and never a pydantic ``ValidationError`` escaping
the handler.

Semantics, preserved from the routes' earlier inline validation:
* ``get_json(silent=True)`` returning ``None`` (missing body OR malformed
  JSON) -> ``400 Bad Request`` "Request body is required".
* any non-``None`` body, including an empty ``{}``, falls through to schema
  validation -> ``422`` on failure (an empty object is a missing-fields
  validation error, not a "missing body").
* a non-object JSON body (array/string/number) is rejected by pydantic with
  its standard error; ``require_object=True`` (admin settings) replaces that
  with the clearer "Request body must be a JSON object" message.

The decorator reads the body *inside* the wrapped endpoint's request context,
so it composes with the auth/rate-limit decorators in any stack order — an
unauthenticated request still 401s before the body is ever validated, exactly
as it did when each handler validated its own body.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from pydantic import BaseModel
from quart import request

from api.jwt import problem_json

# H14: cap the 422 detail so a future validator raising an exception with
# internal data can never dump an unbounded string into the response body.
_MAX_ERROR_DETAIL = 500


def _format_validation_error(exc: Exception) -> str:
    """Build a compact, client-safe message from a pydantic ``ValidationError``.

    Uses ``exc.errors()`` and keeps only ``loc`` / ``msg`` / a repr of the
    offending *input value* — never tracebacks, never internal context dicts.
    Falls back to a capped ``str(exc)`` for non-pydantic exceptions.
    """
    errors_fn = getattr(exc, "errors", None)
    if callable(errors_fn):
        try:
            raw = list(errors_fn())
            parts: list[str] = []
            for err in raw[:5]:
                loc = ".".join(str(p) for p in err.get("loc", ()) ) or "body"
                msg = str(err.get("msg", "invalid value"))
                inp = err.get("input")
                if isinstance(inp, (dict, list)):
                    parts.append(f"{loc}: {msg}")
                else:
                    parts.append(f"{loc}: {msg} (input={inp!r})")
            detail = "; ".join(parts)
            if len(raw) > 5:
                detail += f"; (+{len(raw) - 5} more error(s))"
            return detail[:_MAX_ERROR_DETAIL]
        except Exception:  # noqa: BLE001 — formatting must never 500
            pass
    return str(exc)[:_MAX_ERROR_DETAIL]


def validate_body(
    schema: type[BaseModel],
    *,
    require_object: bool = False,
) -> Callable:
    """Validate the JSON request body against ``schema`` before the route runs.

    On success stores the validated model as ``request._validated`` (read back
    inside the route with :func:`validated_body`). On a missing/malformed body
    returns ``400``; on a schema violation returns RFC 7807 ``422`` with
    pydantic's error text. With ``require_object=True`` a non-object body
    returns the clearer 422 message instead of pydantic's dictionary error.
    """

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def decorated(*args: Any, **kwargs: Any) -> Any:
            body = await request.get_json(silent=True)
            if body is None:
                return problem_json(400, "Bad Request", "Request body is required")
            if require_object and not isinstance(body, dict):
                return problem_json(
                    422, "Unprocessable Entity", "Request body must be a JSON object"
                )
            try:
                data = schema.model_validate(body)
            except Exception as exc:  # noqa: BLE001 - any schema error is a 422
                return problem_json(
                    422, "Unprocessable Entity", _format_validation_error(exc)
                )
            request._validated = data
            return await f(*args, **kwargs)

        return decorated

    return decorator


def validated_body() -> Any:
    """Return the model stored by :func:`validate_body` in the current request.

    Call inside the wrapped route function: the validated model lives on the
    request object of the current request context.

    L14: raises ``RuntimeError`` when the route has no ``@validate_body``
    decorator (or it did not run) — silently returning ``None`` used to let a
    handler proceed on unvalidated data after a decorator-ordering mistake.
    """
    body = getattr(request, "_validated", None)
    if body is None:
        raise RuntimeError(
            "validated_body() called on a request with no validated body — "
            "the route is missing the @validate_body(...) decorator"
        )
    return body
