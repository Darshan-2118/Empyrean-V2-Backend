"""
Health check — validates that the entire backend stack is wired correctly.

Checks:
1. Python environment and imports
2. PostgreSQL connection
3. Database exists
4. All required tables exist
5. Alembic migration is applied (correct revision)
6. Seed data present (admin user, default settings, sample node)
7. App factory loads without errors

Usage::

    python scripts/check_health.py

Exit code 0 = everything OK.
Exit code 1 = something failed (details printed).
"""

import sys
from pathlib import Path

# Make the project root importable
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

PASS = "[OK]"
FAIL = "[FAIL]"
SEP = "-" * 60


def check(description: str, ok: bool, detail: str = ""):
    """Print a check result and return ``ok``."""
    icon = PASS if ok else FAIL
    msg = f"  {icon}  {description}"
    if detail:
        msg += f"  -  {detail}"
    print(msg)
    return ok


def main() -> bool:
    all_ok = True

    # -- 1. Python environment & imports -------------------------------------
    print(f"\n[1/7] Environment & Imports")
    print(SEP)

    # Python version
    py_ok = sys.version_info >= (3, 12)
    all_ok &= check("Python >= 3.12", py_ok, sys.version.split()[0])

    # Model imports (broad except: a config error here is still a FAIL, not a crash)
    try:
        from models import Base, User, Node, SensorReading, Alert, SystemSetting  # noqa: F401
        all_ok &= check("Model imports", True, "all 7 models loaded")
    except Exception as e:
        all_ok &= check("Model imports", False, str(e))

    # -- 2. Database connection -----------------------------------------------
    print(f"\n[2/7] Database")
    print(SEP)

    try:
        from sqlalchemy import inspect, text as sa_text
        from scripts.db_utils import make_engine

        engine = make_engine()

        # Quick connection test
        with engine.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
        all_ok &= check("PostgreSQL connection", True, str(engine.url).rsplit("@", 1)[-1])

        # Database version
        with engine.connect() as conn:
            version = conn.execute(sa_text("SELECT version()")).scalar()
        all_ok &= check(
            "PostgreSQL version",
            "PostgreSQL" in (version or ""),
            (version or "").split(",")[0].strip() if version else "unknown",
        )

        # -- 3. Tables exist --------------------------------------------------
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())

        expected_tables = {
            "users",
            "refresh_tokens",
            "nodes",
            "sensor_readings",
            "hourly_agg",
            "alerts",
            "system_settings",
        }
        missing_tables = expected_tables - existing_tables

        if missing_tables:
            all_ok &= check("All tables exist", False, f"missing: {', '.join(sorted(missing_tables))}")
        else:
            all_ok &= check("All tables exist", True, f"{len(expected_tables)} of {len(expected_tables)} present")

        # -- 4. Indexes exist -------------------------------------------------
        if not missing_tables:
            sensor_indexes = {idx["name"] for idx in inspector.get_indexes("sensor_readings")}
            required_sensor_indexes = {"idx_readings_node_time", "idx_readings_time"}
            missing_sensor_indexes = required_sensor_indexes - sensor_indexes
            if missing_sensor_indexes:
                all_ok &= check("Sensor reading indexes", False, f"missing: {', '.join(sorted(missing_sensor_indexes))}")
            else:
                all_ok &= check("Sensor reading indexes", True, "idx_readings_node_time, idx_readings_time")

        # -- 5. Alembic migration ---------------------------------------------
        try:
            from alembic.config import Config as AlembicConfig
            from alembic.script import ScriptDirectory

            alembic_cfg = AlembicConfig(
                str(Path(__file__).resolve().parents[1] / "alembic.ini")
            )
            head = ScriptDirectory.from_config(alembic_cfg).get_current_head()

            with engine.connect() as conn:
                has_alembic = conn.execute(
                    sa_text("SELECT to_regclass('alembic_version') IS NOT NULL")
                ).scalar()
            if not has_alembic:
                all_ok &= check(
                    "Alembic migration applied", False,
                    "no alembic_version table (run `alembic upgrade head`)",
                )
            else:
                with engine.connect() as conn:
                    db_rev = conn.execute(
                        sa_text("SELECT version_num FROM alembic_version")
                    ).scalar()
                if db_rev == head:
                    all_ok &= check(
                        "Alembic migration applied", True,
                        f"revision {db_rev} (up to date)",
                    )
                else:
                    all_ok &= check(
                        "Alembic migration applied", False,
                        f"database at {db_rev}, expected {head}",
                    )
        except Exception as e:
            all_ok &= check("Alembic migration applied", False, str(e))

        # -- 6. Seed data -----------------------------------------------------
        print(f"\n[3/7] Seed Data")
        print(SEP)

        with engine.connect() as conn:
            # Check admin user exists
            admin_count = conn.execute(
                sa_text("SELECT COUNT(*) FROM users WHERE username = 'admin' AND role = 'admin'")
            ).scalar()
            all_ok &= check(
                "Admin user exists",
                admin_count > 0,
                f"found {admin_count} admin user(s)" if admin_count else "not found",
            )

            # Check sample node exists
            node_count = conn.execute(
                sa_text("SELECT COUNT(*) FROM nodes WHERE node_id = 'ESP32-01'")
            ).scalar()
            all_ok &= check(
                "Sample node exists",
                node_count > 0,
                f"found node ESP32-01" if node_count else "not found",
            )

            # Check default settings exist
            setting_count = conn.execute(
                sa_text("SELECT COUNT(*) FROM system_settings")
            ).scalar()
            all_ok &= check(
                "Default settings seeded",
                setting_count >= 3,
                f"found {setting_count} setting(s)" if setting_count else "not found",
            )

        engine.dispose()

    except Exception as e:
        all_ok &= check("Database checks", False, str(e))

    # -- 7. App factory -------------------------------------------------------
    print(f"\n[4/7] App Factory")
    print(SEP)

    try:
        from app import create_app

        app = create_app()
        all_ok &= check("App factory", True, "Quart app created")
    except Exception as e:
        all_ok &= check("App factory", False, str(e))

    # -- Summary -------------------------------------------------------------
    print()
    print(SEP)
    if all_ok:
        print(f"  {PASS}  ALL CHECKS PASSED")
    else:
        print(f"  {FAIL}  SOME CHECKS FAILED - see details above")
    print(SEP)

    return all_ok


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
