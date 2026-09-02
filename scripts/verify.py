#!/usr/bin/env python3
"""
Empyrean -- full-stack verification script.

Runs every check that matters before you commit, deploy, or start coding:
  1.  PostgreSQL is reachable
  2.  Alembic migration is up to date
  3.  Health check (tables, seed data, app factory)
  4.  pytest (unit / integration tests)  [opt-in, pass --full]

Usage:
    python scripts/verify.py            quick checks only (no pytest)
    python scripts/verify.py --full     full suite including pytest
    python scripts/verify.py --quick    alias for default (backwards compat)

Exit codes:
    0  - everything passed
    1  - one or more checks failed
"""

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

# Pure ASCII for cross-platform compatibility (cmd, powershell, bash, linux)
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
BOLD = "\033[1m"
NC = "\033[0m"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def pass_msg(msg: str):
    print(f"  {GREEN}OK{NC}  {msg}")


def fail_msg(msg: str):
    print(f"  {RED}FAIL{NC}  {msg}")


def header(msg: str):
    print(f"\n{BOLD}-- {msg} --{NC}")


# ── 1. PostgreSQL reachable ──────────────────────────────────────────────────
def check_postgresql() -> bool:
    header("1/4  PostgreSQL")
    try:
        from sqlalchemy import text as sa_text
        from scripts.db_utils import make_engine

        engine = make_engine()
        with engine.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
        engine.dispose()
        pass_msg("PostgreSQL is reachable")
        return True
    except Exception as e:
        fail_msg(f"PostgreSQL connection failed: {e}")
        return False


# ── 2. Alembic up to date ────────────────────────────────────────────────────
def check_migrations() -> bool:
    header("2/4  Migrations")
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from sqlalchemy import text as sa_text
        from scripts.db_utils import make_engine

        alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        # alembic.ini keeps script_location relative; pin it to the repo so the
        # check works from any CWD.
        alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        head = ScriptDirectory.from_config(alembic_cfg).get_current_head()

        engine = make_engine()
        with engine.connect() as conn:
            db_rev = conn.execute(
                sa_text("SELECT version_num FROM alembic_version")
            ).scalar()
        engine.dispose()

        if db_rev == head:
            pass_msg(f"Alembic migration is up to date (revision {head})")
            return True
        fail_msg(f"Database is at revision {db_rev}, expected {head}")
        return False
    except Exception as e:
        fail_msg(f"Alembic check failed: {e}")
        return False


# ── 3. Health check ──────────────────────────────────────────────────────────
def check_health() -> bool:
    header("3/4  Health check")
    try:
        from scripts import check_health

        result = check_health.main()
        if result:
            pass_msg("All health checks passed")
            return True
        else:
            fail_msg("Health check reported failures (see above)")
            return False
    except Exception as e:
        fail_msg(f"Health check crashed: {e}")
        return False


# ── 4. pytest ────────────────────────────────────────────────────────────────
def check_tests() -> bool:
    header("4/4  Tests")
    if importlib.util.find_spec("pytest") is None:
        fail_msg(
            "pytest is not installed — run `pip install -r requirements.txt` "
            "inside the venv, or omit --full to skip the test suite"
        )
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        fail_msg("pytest timed out after 900s")
        return False
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            print(f"    {line}")

    if result.returncode == 0:
        pass_msg("All tests passed")
        return True
    else:
        fail_msg("Some tests failed - run 'pytest tests/ -v' for details")
        if result.stderr:
            for line in result.stderr.strip().split("\n")[-5:]:
                print(f"    {line}")
        return False


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Run pytest only when explicitly requested via --full.
    # Default is a quick liveness check (infrastructure + app factory).
    parser = argparse.ArgumentParser(
        description="Run the Empyrean verification checks (PostgreSQL, "
        "migrations, health check; optionally the pytest suite).",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="also run the pytest suite",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="alias for the default (backwards compat)",
    )
    args = parser.parse_args()
    run_tests = args.full
    results: list[tuple[str, bool | None]] = []

    results.append(("PostgreSQL", check_postgresql()))
    results.append(("Migrations", check_migrations()))
    results.append(("Health", check_health()))

    if run_tests:
        results.append(("Tests", check_tests()))
    else:
        results.append(("Tests", None))  # skipped by default

    # ── Summary ──────────────────────────────────────────────────────────────
    all_pass = all(r is True for _, r in results if r is not None)
    print(f"\n{BOLD}{'=' * 45}{NC}")
    if all_pass:
        print(f"  {GREEN}{BOLD}ALL CHECKS PASSED{NC}")
    else:
        print(f"  {RED}{BOLD}SOME CHECKS FAILED{NC}")
    print(f"{BOLD}{'=' * 45}{NC}")

    for name, result in results:
        if result is True:
            print(f"  {GREEN}OK{NC}   {name}")
        elif result is False:
            print(f"  {RED}FAIL{NC} {name}")
        else:
            skipped_hint = "  (pass --full to run)" if name == "Tests" else ""
            print(f"  {YELLOW}--{NC}   {name} (skipped){skipped_hint}")

    print("")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted")
        sys.exit(130)
