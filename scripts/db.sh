#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Empyrean — database helper for local development.
#
# Reads credentials from .env so you never type a password on the CLI.
#
# Usage:
#   ./scripts/db.sh connect          — open an interactive psql shell
#   ./scripts/db.sh sql "..."        — run a one-off query
#   ./scripts/db.sh tables           — list all tables
#   ./scripts/db.sh hypertables      — list TimescaleDB hypertables
#   ./scripts/db.sh migrate          — run alembic migrations
#   ./scripts/db.sh seed             — seed the database
#   ./scripts/db.sh check            — run the health-check script
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail
cd "$(dirname "$0")/.."

# ── Load DATABASE_URL from .env ──────────────────────────────────────────────
DB_URL_LINE="$(grep -E '^DATABASE_URL=' .env 2>/dev/null | head -1 || true)"
if [[ -z "${DB_URL_LINE:-}" ]]; then
  echo "ERROR: DATABASE_URL not found in .env" >&2
  exit 1
fi
export "$DB_URL_LINE"

# psql accepts a full connection URI, so no manual URL parsing is needed
# (this also handles the password correctly without exposing it on the CLI).
_PSQL=(psql "$DATABASE_URL")

case "${1:-help}" in
  connect)
    exec "${_PSQL[@]}"
    ;;
  sql)
    shift
    exec "${_PSQL[@]}" -c "$*"
    ;;
  tables)
    exec "${_PSQL[@]}" -c "\dt"
    ;;
  hypertables)
    exec "${_PSQL[@]}" -c "SELECT * FROM timescaledb_information.hypertables"
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  seed)
    exec python scripts/seed.py
    ;;
  check)
    exec python scripts/check_health.py
    ;;
  *)
    echo "Usage: $0 {connect|sql|tables|hypertables|migrate|seed|check}"
    exit 1
    ;;
esac
