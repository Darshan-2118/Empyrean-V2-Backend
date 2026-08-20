import os

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