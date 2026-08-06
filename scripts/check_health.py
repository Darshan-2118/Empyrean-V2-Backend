"""
Health check — validates that the entire backend stack is wired correctly.

Checks (numbered to match the output):
1. Python environment and imports
2. PostgreSQL connection
3. All required tables exist
4. Sensor-reading indexes exist
5. Alembic migration is applied (correct revision)
6. ``sensor_readings`` is a real TimescaleDB hypertable (queried)
7. Redis is reachable (real PING)
8. Seed data present — only when ``APP_ENV == "development"``
   (a healthy production DB is not required to carry the dev seed rows)
9. App factory loads without errors

Requires ``Python >= 3.12`` (type-hint syntax used across the repo).

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

from sqlalchemy import inspect, text as sa_text  # noqa: E402

from config import get_config  # noqa: E402

PASS = "[OK]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"
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
    cfg = get_config()
    is_dev = cfg.APP_ENV == "development"
    all_ok = True
    total = 9

    # -- 1. Python environment & imports -------------------------------------
    print(f"\n[1/{total}] Environment & Imports")
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
    # Isolated so a DB outage degrades the *dependent* sections (3–6, 8) to a
    # SKIP while Redis (7) and the app factory (9) still get reported (N-7),
    # instead of collapsing every check into one generic "Database checks" FAIL.
    print(f"\n[2/{total}] Database")
    print(SEP)

    engine = None
    db_ok = False
    try:
        from scripts.db_utils import make_engine

        engine = make_engine()

        # Quick connection test
        with engine.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
        all_ok &= check("PostgreSQL connection", True, str(engine.url).rsplit("@", 1)[-1])
        db_ok = True

        # Database version
        with engine.connect() as conn:
            version = conn.execute(sa_text("SELECT version()")).scalar()
        all_ok &= check(
            "PostgreSQL version",
            "PostgreSQL" in (version or ""),
            (version or "").split(",")[0].strip() if version else "unknown",
        )
    except Exception as e:
        all_ok &= check("PostgreSQL connection", False, f"DB unreachable: {e}")

    # -- 3. Tables exist ------------------------------------------------------
    print(f"\n[3/{total}] Tables")
    print(SEP)
    missing_tables: set[str] = set()
    if not db_ok:
        print(f"  {SKIP}  Table checks skipped (database connection failed)")
    else:
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

    # -- 4. Indexes exist -----------------------------------------------------
    print(f"\n[4/{total}] Indexes")
    print(SEP)
    if not db_ok or missing_tables:
        reason = "database connection failed" if not db_ok else "tables missing"
        print(f"  {SKIP}  Sensor reading index checks skipped ({reason})")
    else:
        inspector = inspect(engine)
        sensor_indexes = {idx["name"] for idx in inspector.get_indexes("sensor_readings")}
        required_sensor_indexes = {"idx_readings_node_time", "idx_readings_time"}
        missing_sensor_indexes = required_sensor_indexes - sensor_indexes
        if missing_sensor_indexes:
            all_ok &= check("Sensor reading indexes", False, f"missing: {', '.join(sorted(missing_sensor_indexes))}")
        else:
            all_ok &= check("Sensor reading indexes", True, "idx_readings_node_time, idx_readings_time")

    # -- 5. Alembic migration -------------------------------------------------
    print(f"\n[5/{total}] Alembic Migration")
    print(SEP)
    if not db_ok:
        print(f"  {SKIP}  Alembic check skipped (database connection failed)")
    else:
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

    # -- 6. Hypertable (real query, not inferred) -----------------------------
    print(f"\n[6/{total}] Hypertable")
    print(SEP)
    if not db_ok:
        print(f"  {SKIP}  Hypertable check skipped (database connection failed)")
    else:
        try:
            with engine.connect() as conn:
                hypertable = conn.execute(
                    sa_text(
                        "SELECT hypertable_name FROM timescaledb_information.hypertables "
                        "WHERE hypertable_name = 'sensor_readings'"
                    )
                ).scalar()
            all_ok &= check(
                "sensor_readings is a hypertable",
                hypertable == "sensor_readings",
                "sensor_readings" if hypertable == "sensor_readings"
                else "not a hypertable (run `alembic upgrade head`)",
            )
        except Exception as e:
            all_ok &= check(
                "sensor_readings is a hypertable", False,
                f"could not query hypertables: {e}",
            )

    # -- 7. Redis ---------------------------------------------------------------
    print(f"\n[7/{total}] Redis")
    print(SEP)
    try:
        from redis import Redis

        client = Redis.from_url(cfg.REDIS_URL, socket_connect_timeout=3)
        try:
            pong = client.ping()
            all_ok &= check(
                "Redis reachable", bool(pong),
                cfg.REDIS_URL if pong else "PING failed",
            )
        finally:
            client.close()
    except Exception as e:
        all_ok &= check("Redis reachable", False, str(e))

    # -- 8. Seed data (development only) --------------------------------------
    print(f"\n[8/{total}] Seed Data")
    print(SEP)

    if not is_dev:
        print(
            f"  {SKIP}  Seed checks skipped (APP_ENV={cfg.APP_ENV!r} — "
            "not development)"
        )
    elif not db_ok:
        print(f"  {SKIP}  Seed checks skipped (database connection failed)")
    else:
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

    if engine is not None:
        engine.dispose()

    # -- 9. App factory ---------------------------------------------------------
    print(f"\n[9/{total}] App Factory")
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