"""
Seed script — populates the database with initial data for development.

Creates:
- Admin user: ``admin`` / ``admin@empyrean.local`` (password: ``admin123``)
- Default system settings (AQI thresholds, alerts toggle)
- A sample node (``ESP32-01``)

Usage::

    python scripts/seed.py
"""

import logging
import sys
from pathlib import Path

# Make the project root importable (works regardless of CWD)
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _PROJECT_ROOT)

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


def seed():
    with get_sync_db() as session:
        # ── 1. Admin user ─────────────────────────────────────────────────
        existing_admin = session.query(User).filter_by(username="admin").first()
        if existing_admin:
            logger.info("Admin user already exists — skipping.")
        else:
            admin = User(
                username="admin",
                email="admin@empyrean.local",
                password_hash=hash_password("admin123"),
                role="admin",
                is_active=True,
                notification_prefs={"email_on_critical": True},
            )
            session.add(admin)
            logger.info("Created admin user: admin / admin123")

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
                key="alerts_enabled",
                value="true",
                description="Master toggle for alert generation",
            ),
        ]
        for setting in defaults:
            existing = session.query(SystemSetting).filter_by(key=setting.key).first()
            if existing:
                logger.info("Setting '%s' already exists — skipping.", setting.key)
            else:
                session.add(setting)
                logger.info("Created setting: %s = %s", setting.key, setting.value)

        # ── 3. Sample node ────────────────────────────────────────────────
        existing_node = session.query(Node).filter_by(node_id="ESP32-01").first()
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
    try:
        seed()
    except Exception as e:
        logger.exception("Seed failed: %s", e)
        sys.exit(1)

