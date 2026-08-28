"""
Seed script — populates the database with initial data for development.

Creates:
- Admin user: ``admin`` / ``admin@empyrean.local`` — only when the env
  ``SEED_ADMIN_PASSWORD`` is set (otherwise skipped; use the interactive
  ``scripts/create_admin.py`` instead)
- Default system settings (AQI thresholds, data retention, alerts toggle)
- A sample node (``ESP32-01``)

Safety:
- Refuses to run against ``APP_ENV=production`` unless ``--force`` is given.
- Reads the admin password from ``SEED_ADMIN_PASSWORD``; never logs the
  plaintext password, only the username.

Usage::

    python scripts/seed.py                                     # settings + sample node
    SEED_ADMIN_PASSWORD=<secret> python scripts/seed.py        # also creates 'admin'
    SEED_ADMIN_PASSWORD=<secret> python scripts/seed.py --force   # prod override
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# Make the project root importable (works regardless of CWD)
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _PROJECT_ROOT)

from config import get_config

from sqlalchemy import func, select

from models import (
    Node,
    SystemSetting,
    User,
    get_sync_db,
)
from models.helpers import hash_password

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("seed")

_ERR_EXIT = 3


def _admin_password() -> str | None:
    """Return the admin password from ``SEED_ADMIN_PASSWORD``, or ``None``.

    When unset, admin creation is skipped — interactive setups should use
    ``python scripts/create_admin.py`` instead of wiring env vars.
    """
    password = os.environ.get("SEED_ADMIN_PASSWORD")
    if not password:
        logger.info(
            "SEED_ADMIN_PASSWORD not set — skipping admin user creation. "
            "Run `python scripts/create_admin.py` to create your admin "
            "account interactively."
        )
        return None
    return password


def seed(force: bool = False) -> None:
    cfg = get_config()

    # ── Production guard ──────────────────────────────────────────────────
    # Seeding a production DB is destructive and would mint a known account,
    # so it is refused unless the operator explicitly passes --force.
    if cfg.APP_ENV == "production" and not force:
        logger.error(
            "Refusing to seed the '%s' database. Set APP_ENV=development, or "
            "re-run with --force if you really intend to seed production.",
            cfg.APP_ENV,
        )
        sys.exit(_ERR_EXIT)

    admin_password = _admin_password()

    with get_sync_db() as session:
        # ── 1. Admin user ─────────────────────────────────────────────────
        # L34: 2.x-style select() queries, matching the rest of the codebase
        # (the legacy 1.x query()/filter_by() chain is deprecated).
        if admin_password is None:
            pass  # interactive path — see scripts/create_admin.py
        else:
            existing_admin = session.scalar(
                select(User).where(User.username == "admin")
            )
            if existing_admin:
                logger.info("Admin user already exists — skipping.")
            else:
                admin = User(
                    username="admin",
                    email="admin@empyrean.local",
                    password_hash=hash_password(admin_password),
                    role="admin",
                    is_active=True,
                    notification_prefs={"email_on_critical": True},
                )
                session.add(admin)
                # Log the username only — never the plaintext password.
                logger.info("Created admin user: '%s' (password from SEED_ADMIN_PASSWORD)", admin.username)

        # ── 1b. Bootstrap admin from env (H28) ────────────────────────────
        # The hardcoded "Darshan"/"Darsh1812" row is removed. Operators who
        # want a second named admin set BOOTSTRAP_ADMIN_USERNAME/PASSWORD and
        # the API provisions it at startup — never with a source-committed
        # password.
        cfg_creds = (
            os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "").strip(),
            os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", ""),
            os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip(),
        )
        if cfg_creds[0] and cfg_creds[1]:
            # L73: EXACT match only, same semantics as the API's startup
            # provisioning — the old ilike lookup let _/% wildcards in
            # BOOTSTRAP_ADMIN_USERNAME force-promote unrelated users.
            existing_bootstrap = session.scalar(
                select(User).where(User.username == cfg_creds[0])
            )
            if existing_bootstrap:
                if existing_bootstrap.role != "admin" or not existing_bootstrap.is_active:
                    existing_bootstrap.role = "admin"
                    existing_bootstrap.is_active = True
                    session.commit()
                logger.info("Bootstrap admin user '%s' already exists — ensured admin role.", cfg_creds[0])
            else:
                case_variant = session.scalar(
                    select(User.username)
                    .where(func.lower(User.username) == cfg_creds[0].lower())
                    .limit(1)
                )
                if case_variant is not None:
                    logger.error(
                        "Bootstrap admin '%s' has no exact match but case-variant "
                        "user '%s' exists — refusing to promote it.",
                        cfg_creds[0], case_variant,
                    )
                else:
                    bootstrap = User(
                        username=cfg_creds[0],
                        email=cfg_creds[2] or f"{cfg_creds[0].lower()}@empyrean.local",
                        password_hash=hash_password(cfg_creds[1]),
                        role="admin",
                        is_active=True,
                        notification_prefs={"email_on_critical": True},
                    )
                    session.add(bootstrap)
                    logger.info("Created bootstrap admin user: '%s' (password from BOOTSTRAP_ADMIN_PASSWORD)", cfg_creds[0])
        else:
            logger.info(
                "BOOTSTRAP_ADMIN_USERNAME/PASSWORD not both set — skipping "
                "bootstrap admin seed."
            )

        # ── 2. Default system settings ────────────────────────────────────
        defaults = [
            SystemSetting(
                key="aqi_warning_threshold",
                value="100",
                description="AQI value that triggers a warning alert",
            ),
            SystemSetting(
                key="aqi_critical_threshold",
                value="150",
                description="AQI value that triggers a critical alert",
            ),
            SystemSetting(
                key="data_retention_days",
                value="365",
                description="How long raw readings are retained before purging",
            ),
            SystemSetting(
                key="alerts_enabled",
                value="true",
                description="Master toggle for alert generation",
            ),
        ]
        for setting in defaults:
            existing = session.scalar(
                select(SystemSetting).where(SystemSetting.key == setting.key)
            )
            if existing:
                logger.info("Setting '%s' already exists — skipping.", setting.key)
            else:
                session.add(setting)
                logger.info("Created setting: %s = %s", setting.key, setting.value)

        # ── 3. Sample node ────────────────────────────────────────────────
        existing_node = session.scalar(
            select(Node).where(Node.node_id == "ESP32-01")
        )
        if existing_node:
            logger.info("Node 'ESP32-01' already exists — skipping.")
        else:
            node = Node(
                node_id="ESP32-01",
                name="Downtown Sensor",
                location_name="City Center, Main Street",
                lat=28.6139,
                lon=77.2090,
                firmware_version="v2.1.0",
                reading_interval=30,
                is_active=True,
            )
            session.add(node)
            logger.info("Created sample node: ESP32-01")

    logger.info("Seed complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed the Empyrean database with an admin user, "
        "default settings, and a sample node.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow seeding even when APP_ENV=production (use with care)",
    )
    args = parser.parse_args()

    try:
        seed(force=args.force)
    except Exception as e:
        logger.exception("Seed failed: %s", e)
        sys.exit(1)

