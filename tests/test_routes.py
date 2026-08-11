"""Route registration tests — documented endpoint paths must be registered.

These assert against the real app's URL map, so a blueprint ``url_prefix`` /
route mismatch that silently serves the wrong path (and 404s the documented
one) is caught.  No DB, Redis, or fixtures: ``create_app()`` only builds the
app and registers blueprints — the SQLAlchemy engines are lazy and the
``before_serving`` DB check runs only on serve.
"""

from app import create_app


def test_forecast_registered_at_documented_path():
    """``GET /api/v1/forecast`` must be registered (not a doubled prefix).

    Regression: the forecast blueprint is registered under
    ``url_prefix="/api/v1/forecast"`` (app.py), so its route must be ``""``
    (the pattern ``api/profile.py`` uses) rather than ``"/forecast"`` —
    otherwise the registered path is the doubled ``/api/v1/forecast/forecast``
    and the documented endpoint returns 404.
    """
    app = create_app()
    rendered = {str(r) for r in app.url_map.iter_rules()}

    assert any("/api/v1/forecast" in s for s in rendered)
    assert not any("/api/v1/forecast/forecast" in s for s in rendered)


def test_export_registered_at_documented_path():
    """``GET /api/v1/export`` must be registered (not a doubled prefix).

    Regression mirroring ``test_forecast_registered_at_documented_path``: the
    export blueprint is registered under ``url_prefix="/api/v1/export"``
    (app.py), so its route must be ``""`` (the bare-prefix form) rather than
    ``"/export"`` — otherwise the registered path is the doubled
    ``/api/v1/export/export`` and the documented endpoint returns 404.
    """
    app = create_app()
    rendered = {str(r) for r in app.url_map.iter_rules()}

    assert any("/api/v1/export" in s for s in rendered)
    assert not any("/api/v1/export/export" in s for s in rendered)
