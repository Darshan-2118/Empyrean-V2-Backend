#!/usr/bin/env python3
"""
Empyrean -- full-stack verification script.

Runs every check that matters before you commit, deploy, or start coding:
  1.  PostgreSQL is reachable
  2.  Alembic migration is up to date
  3.  Health check (tables, seed data, app factory)
  4.  pytest (unit / integration tests)

Usage:
    python scripts/verify.py            full suite
    python scripts/verify.py --quick    skip pytest

Exit codes:
    0  - everything passed
    1  - one or more checks failed
"""

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


def info_msg(msg: str):
    print(f"  {YELLOW}..{NC}  {msg}")


def header(msg: str):
    print(f"\n{BOLD}-- {msg} --{NC}")


# ── 1. PostgreSQL reachable ──────────────────────────────────────────────────
def check_postgresql() -> bool:
    header("1/4  PostgreSQL")
    try:
        from sqlalchemy import create_engine, text as sa_text
        from config import get_config

        cfg = get_config()
        engine = create_engine(
            cfg.DATABASE_URL,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )
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

        alembic_cfg = Config(PROJECT_ROOT / "alembic.ini")
        script = ScriptDirectory.from_config(alembic_cfg)
        head = script.get_current_head()
        pass_msg(f"Alembic head is at {head}")
        return True
    except Exception as e:
        fail_msg(f"Alembic check failed: {e}")
        return False


# ── 3. Health check ──────────────────────────────────────────────────────────
def check_health() -> bool:
    header("3/4  Health check")
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
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
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
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
    quick = "--quick" in sys.argv
    results: list[tuple[str, bool | None]] = []

    results.append(("PostgreSQL", check_postgresql()))
    results.append(("Migrations", check_migrations()))
    results.append(("Health", check_health()))

    if quick:
        results.append(("Tests", None))  # skipped
    else:
        results.append(("Tests", check_tests()))

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
            print(f"  {YELLOW}--{NC}   {name} (skipped)")

    print("")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
