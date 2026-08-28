"""Production secret-guard tests (H-7).

The shipped / weak secrets must be rejected in production, while a strong secret
is accepted. Constructing ``Config`` with explicit init args lets us override the
repo ``.env`` for a clean, isolated check.
"""

import pytest

from config import Config, _is_weak_secret, get_config, reset_config_cache

_STRONG_SECRET_KEY = "x9#kP1@qZ4!wL7^mN2&bV5*cR8+dF0*Qw"
_STRONG_JWT = "y8#jM2@rW5!qT9^pB3&cX6*zN0+vG1+Zz"
# Long enough to pass the length gate, but worth rejecting for coding-style
# reasons only if the placeholder is also blocked.
_JWT_NEUTRAL = "n5#gK3@cR8!wB6^zQ1&vM4*tP9+dX2*Aa"


def test_placeholder_secret_key_is_blocked():
    assert _is_weak_secret("change-me-to-a-random-secret") is True


def test_short_or_empty_secrets_are_blocked():
    assert _is_weak_secret("") is True
    assert _is_weak_secret("a") is True
    assert _is_weak_secret("a" * 31) is True


def test_long_low_entropy_secrets_are_blocked():
    assert _is_weak_secret("a" * 32) is True
    assert _is_weak_secret("ab" * 16) is True  # 32 bytes but 2 distinct chars
    assert _is_weak_secret("1234" * 8) is True  # 4 distinct chars


def test_strong_secret_passes():
    assert _is_weak_secret(_STRONG_SECRET_KEY) is False


def test_production_rejects_placeholder_secret_key():
    with pytest.raises(ValueError):
        Config(
            APP_ENV="production",
            SECRET_KEY="change-me-to-a-random-secret",
            JWT_SECRET=_JWT_NEUTRAL,
        )


def test_production_rejects_empty_jwt_secret():
    with pytest.raises(ValueError):
        Config(
            APP_ENV="production",
            SECRET_KEY=_STRONG_SECRET_KEY,
            JWT_SECRET="",
        )


def test_production_rejects_single_char_jwt_secret():
    with pytest.raises(ValueError):
        Config(
            APP_ENV="production",
            SECRET_KEY=_STRONG_SECRET_KEY,
            JWT_SECRET="a",
        )


def test_production_accepts_strong_secrets():
    cfg = Config(
        APP_ENV="production",
        SECRET_KEY=_STRONG_SECRET_KEY,
        JWT_SECRET=_STRONG_JWT,
    )
    assert cfg.APP_ENV == "production"


_STRONG_BOOTSTRAP = "S3cure!bootstrap#Passphrase$2024x"


def test_bootstrap_password_placeholder_blocked_in_all_environments():
    """L66: known dev placeholders are rejected even outside production."""
    for env in ("development", "test"):
        with pytest.raises(ValueError):
            Config(
                APP_ENV=env,
                SECRET_KEY=_STRONG_SECRET_KEY,
                JWT_SECRET=_STRONG_JWT,
                BOOTSTRAP_ADMIN_PASSWORD="dev-secret-key",
            )


def test_bootstrap_password_weak_rejected_outside_test_env():
    """L66: a set password gets the full strength check in real environments."""
    with pytest.raises(ValueError):
        Config(
            APP_ENV="production",
            SECRET_KEY=_STRONG_SECRET_KEY,
            JWT_SECRET=_STRONG_JWT,
            BOOTSTRAP_ADMIN_PASSWORD="short-pass!",
        )


def test_bootstrap_password_weak_allowed_in_test_env():
    """L66: APP_ENV=test relaxes strength (mirrors SECRET_KEY/JWT_SECRET)."""
    cfg = Config(
        APP_ENV="test",
        SECRET_KEY=_STRONG_SECRET_KEY,
        JWT_SECRET=_STRONG_JWT,
        BOOTSTRAP_ADMIN_PASSWORD="short-pass!",
    )
    assert cfg.BOOTSTRAP_ADMIN_PASSWORD == "short-pass!"


def test_bootstrap_password_empty_is_valid_opt_out():
    """L66: empty stays valid — it disables bootstrap provisioning."""
    cfg = Config(
        APP_ENV="production",
        SECRET_KEY=_STRONG_SECRET_KEY,
        JWT_SECRET=_STRONG_JWT,
        BOOTSTRAP_ADMIN_PASSWORD="",
    )
    assert cfg.BOOTSTRAP_ADMIN_PASSWORD == ""


def test_bootstrap_password_strong_accepted_in_production():
    cfg = Config(
        APP_ENV="production",
        SECRET_KEY=_STRONG_SECRET_KEY,
        JWT_SECRET=_STRONG_JWT,
        BOOTSTRAP_ADMIN_PASSWORD=_STRONG_BOOTSTRAP,
    )
    assert cfg.BOOTSTRAP_ADMIN_PASSWORD == _STRONG_BOOTSTRAP


def test_get_config_caches_but_reset_hook_refreshes(monkeypatch):
    """N-8: get_config() caches; reset_config_cache() forces a re-read.

    The conftest override mechanism relies on the reset hook: conftest builds a
    config, repoints DATABASE_URL, then resets the cache so the models import
    picks up the test URL.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://override:secret@db:5432/other")
    reset_config_cache()
    try:
        assert get_config().DATABASE_URL == "postgresql://override:secret@db:5432/other"
        assert get_config() is get_config()  # cached — same instance
    finally:
        reset_config_cache()  # never leave a poisoned cache for other tests


def test_cors_origins_list_covers_both_dev_frontends():
    """L-32: the CORS default covers both dev frontends, agreeing with docs/api.md.

    ``Config()`` reads ``CORS_ORIGINS`` (default or .env); the split must yield
    both ``http://localhost:3000`` and ``http://localhost:5173`` so neither dev
    UI loses CORS headers.
    """
    assert Config().cors_origins_list == [
        "http://localhost:3000",
        "http://localhost:5173",
    ]