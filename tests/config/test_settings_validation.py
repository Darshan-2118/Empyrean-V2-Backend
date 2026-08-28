import os

import pytest

from config import Config


def test_aqi_warning_must_be_lower_than_critical():
    """#43: Config should reject AQI warning >= critical threshold."""
    os.environ["AQI_WARNING_THRESHOLD"] = "200"
    os.environ["AQI_CRITICAL_THRESHOLD"] = "150"
    try:
        Config()
        assert False, "Expected validation error"
    except ValueError as e:
        assert (
            "AQI_WARNING_THRESHOLD must be less than AQI_CRITICAL_THRESHOLD"
            in str(e)
        )
    finally:
        os.environ.pop("AQI_WARNING_THRESHOLD", None)
        os.environ.pop("AQI_CRITICAL_THRESHOLD", None)


def test_default_aqi_thresholds_are_valid():
    """Default thresholds should pass validation."""
    cfg = Config()
    assert cfg.AQI_WARNING_THRESHOLD < cfg.AQI_CRITICAL_THRESHOLD


def test_database_url_rejects_unusable_schemes():
    """M97: schemes the installed sync driver can't use are rejected."""
    for url in (
        "postgresql+psycopg://user:pass@localhost:5432/airquality",
        "postgresql+asyncpg://user:pass@localhost:5432/airquality",
        "mysql://user:pass@localhost:5432/airquality",
    ):
        try:
            Config(DATABASE_URL=url)
            assert False, f"Expected validation error for {url}"
        except ValueError as e:
            assert "postgresql+psycopg2" in str(e)


def test_database_url_accepts_installed_driver_schemes():
    """M97: postgresql, postgres and postgresql+psycopg2 all pass."""
    for scheme in ("postgresql", "postgres", "postgresql+psycopg2"):
        cfg = Config(DATABASE_URL=f"{scheme}://user:pass@localhost:5432/airquality")
        assert cfg.DATABASE_URL.startswith(f"{scheme}://")


@pytest.mark.parametrize(
    "field",
    [
        "JWT_ACCESS_TOKEN_EXPIRY_MINUTES",
        "JWT_REFRESH_TOKEN_EXPIRY_DAYS",
        "EXPORT_TIMEOUT_SECONDS",
        "MQTT_QUEUE_MAX",
    ],
)
def test_operational_numerics_must_be_positive(field):
    """M98: zero/negative values silently break auth, exports, backpressure."""
    for bad in (0, -1):
        try:
            Config(**{field: bad})
            assert False, f"Expected validation error for {field}={bad}"
        except ValueError as e:
            assert field in str(e)


@pytest.mark.parametrize(
    "field",
    [
        "JWT_ACCESS_TOKEN_EXPIRY_MINUTES",
        "JWT_REFRESH_TOKEN_EXPIRY_DAYS",
        "EXPORT_TIMEOUT_SECONDS",
        "MQTT_QUEUE_MAX",
    ],
)
def test_operational_numerics_accept_positive(field):
    """M98: positive values (including the boundary 1) pass."""
    assert getattr(Config(**{field: 1}), field) == 1