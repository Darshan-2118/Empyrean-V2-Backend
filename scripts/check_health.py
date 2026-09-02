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

PASS = "[OK]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"
SEP = "-" * 60

# A missing dependency is an expected failure for a fresh clone — report it
# actionably instead of dumping an ImportError traceback.
try:
    from sqlalchemy import inspect, text as sa_text  # noqa: E402

    from config import get_config  # noqa: E402
except ImportError as e:
    print(f"{FAIL}  Missing dependency: {e}")
    print("Install the requirements first (e.g. `pip install -r requirements.txt` "
          "inside the .venv).")
    sys.exit(1)


def check(description: str, ok: bool, detail: str = ""):
    """Print a check result and return ``ok``."""
    icon = PASS if ok else FAIL
    msg = f"  {icon}  {description}"
    if detail:
        msg += f"  -  {detail}"
    print(msg)
    return ok


def main() -> bool:
    # A missing/invalid .env must produce a friendly message, not a raw
    # pydantic traceback before any section output is printed.
    try:
        cfg = get_config()
    except Exception as e:
        print(f"\n{FAIL}  Configuration error")
        print(SEP)
        print(f"  {e}")
        print("  Fix: copy .env.example to .env in the project root and set the "
              "required values (see docs/configuration.md).")
        return False

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
        expected_tables = {
            "users",
            "refresh_tokens",
            "password_reset_tokens",
            "nodes",
            "sensor_readings",
            "hourly_agg",
            "alerts",
            "system_settings",
            "audit_logs",
        }
        try:
            inspector = inspect(engine)
            existing_tables = set(inspector.get_table_names())

            missing_tables = expected_tables - existing_tables

            if missing_tables:
                all_ok &= check("All tables exist", False, f"missing: {', '.join(sorted(missing_tables))}")
            else:
                all_ok &= check("All tables exist", True, f"{len(expected_tables)} of {len(expected_tables)} present")
        except Exception as e:
            all_ok &= check("All tables exist", False, f"could not inspect tables: {e}")
            missing_tables = expected_tables  # degrade dependents to SKIP/FAIL, not crash

    # -- 4. Indexes exist -----------------------------------------------------
    print(f"\n[4/{total}] Indexes")
    print(SEP)
    if not db_ok or missing_tables:
        reason = "database connection failed" if not db_ok else "tables missing"
        print(f"  {SKIP}  Sensor reading index checks skipped ({reason})")
    else:
        try:
            inspector = inspect(engine)
            sensor_indexes = {idx["name"] for idx in inspector.get_indexes("sensor_readings")}
            required_sensor_indexes = {"idx_readings_node_time", "idx_readings_time"}
            missing_sensor_indexes = required_sensor_indexes - sensor_indexes
            if missing_sensor_indexes:
                all_ok &= check("Sensor reading indexes", False, f"missing: {', '.join(sorted(missing_sensor_indexes))}")
            else:
                all_ok &= check("Sensor reading indexes", True, "idx_readings_node_time, idx_readings_time")
        except Exception as e:
            all_ok &= check("Sensor reading indexes", False, f"could not inspect indexes: {e}")

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
            # alembic.ini keeps script_location relative; pin it to the repo so
            # the check works from any CWD.
            alembic_cfg.set_main_option(
                "script_location",
                str(Path(__file__).resolve().parents[1] / "migrations"),
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
            # Distinguish "extension not installed" from "table not converted":
            # querying timescaledb_information.* without the extension raises,
            # which previously produced a misleading "run alembic upgrade head".
            with engine.connect() as conn:
                ext_installed = conn.execute(
                    sa_text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
                ).scalar()
            if not ext_installed:
                all_ok &= check(
                    "sensor_readings is a hypertable", False,
                    "timescaledb extension is not installed in this database — "
                    "install it (CREATE EXTENSION timescaledb), then run "
                    "`alembic upgrade head`",
                )
            else:
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
    # L72: redact credentials exactly like the DB check above — the raw
    # REDIS_URL (and any echo of it in exception text) must never be printed.
    redis_redacted = cfg.REDIS_URL.rsplit("@", 1)[-1]
    try:
        from redis import Redis

        client = Redis.from_url(cfg.REDIS_URL, socket_connect_timeout=3)
        try:
            pong = client.ping()
            all_ok &= check(
                "Redis reachable", bool(pong),
                redis_redacted if pong else "PING failed",
            )
        finally:
            client.close()
    except Exception as e:
        all_ok &= check("Redis reachable", False, str(e).replace(cfg.REDIS_URL, redis_redacted))

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
        seed_tables = {"users", "nodes", "system_settings"}
        missing_seed_tables = seed_tables & missing_tables
        if missing_seed_tables:
            print(
                f"  {SKIP}  Seed checks skipped (missing tables: "
                f"{', '.join(sorted(missing_seed_tables))})"
            )
        else:
            try:
                with engine.connect() as conn:
                    # Check admin user exists
                    admin_count = conn.execute(
                        sa_text("SELECT COUNT(*) FROM users WHERE username = 'admin' AND role = 'admin'")
                    ).scalar()
                    # Check sample node exists
                    node_count = conn.execute(
                        sa_text("SELECT COUNT(*) FROM nodes WHERE node_id = 'ESP32-01'")
                    ).scalar()
                    # Check default settings exist
                    setting_count = conn.execute(
                        sa_text("SELECT COUNT(*) FROM system_settings")
                    ).scalar()
            except Exception as e:
                all_ok &= check(
                    "Seed data queries", False,
                    f"could not query seed data: {e} (run `alembic upgrade head` "
                    "and `python scripts/seed.py`)",
                )
            else:
                all_ok &= check(
                    "Admin user exists",
                    admin_count > 0,
                    f"found {admin_count} admin user(s)" if admin_count else "not found",
                )
                all_ok &= check(
                    "Sample node exists",
                    node_count > 0,
                    f"found node ESP32-01" if node_count else "not found",
                )
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
    try:
        success = main()
    except KeyboardInterrupt:
        print("\nAborted")
        sys.exit(130)
    sys.exit(0 if success else 1)
