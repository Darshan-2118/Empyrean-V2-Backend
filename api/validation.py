"""
Pydantic request-body validation middleware (API side).

Decorator ``@validate_body(Schema)`` applied to JSON-body endpoints. It awaits
``request.get_json(silent=True)``, runs ``Schema.model_validate(...)``
(pydantic v2), and stores the validated model on the request where the route
reads it back via :func:`validated_body`. On validation failure it returns an
RFC 7807 ``422 Unprocessable Entity`` (``_problem_json`` with the pydantic
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

from api.jwt import _problem_json


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
                return _problem_json(400, "Bad Request", "Request body is required")
            if require_object and not isinstance(body, dict):
                return _problem_json(
                    422, "Unprocessable Entity", "Request body must be a JSON object"
                )
            try:
                data = schema.model_validate(body)
            except Exception as exc:  # noqa: BLE001 - any schema error is a 422
                return _problem_json(422, "Unprocessable Entity", str(exc))
            request._validated = data
            return await f(*args, **kwargs)

        return decorated

    return decorator


def validated_body() -> Any:
    """Return the model stored by :func:`validate_body` in the current request.

    Call inside the wrapped route function: the validated model lives on the
    request object of the current request context.
    """
    return getattr(request, "_validated", None)
