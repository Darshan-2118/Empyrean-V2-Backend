#!/usr/bin/env bash
set -euo pipefail

# Top-of-file configuration (each overridable via same-named env vars)
SERVER_USER="${SERVER_USER:-empyrean}"
SERVER_HOST="${SERVER_HOST:-}"
APP_DIR="${APP_DIR:-/opt/empyrean}"

if [ -z "$SERVER_HOST" ]; then
  echo "[deploy] SERVER_HOST is not set — export SERVER_HOST before running" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

trap 'echo "[deploy] FAILED — see error output above" >&2' ERR

echo "[deploy] Deploying to $SERVER_USER@$SERVER_HOST:$APP_DIR"

# --- Sync code ---
rsync -az --delete \
  --exclude .git \
  --exclude venv \
  --exclude __pycache__ \
  --exclude .pytest_cache \
  --exclude .env \
  --exclude certs \
  ./ "$SERVER_USER@$SERVER_HOST:$APP_DIR"

# --- Python environment ---
ssh "$SERVER_USER@$SERVER_HOST" bash -s -- "$APP_DIR" <<'REMOTE'
set -e
APP_DIR="$1"
if [ ! -d "$APP_DIR/venv" ]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" --upgrade
REMOTE

# --- Systemd units + nginx ---
ssh "$SERVER_USER@$SERVER_HOST" bash -s -- "$APP_DIR" "${DOMAIN:-}" <<'REMOTE'
set -e
APP_DIR="$1"
DOMAIN="$2"

sudo install -m 0644 "$APP_DIR"/deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload

# Configure Nginx if DOMAIN is provided
if [ -n "$DOMAIN" ] && [ -f "$APP_DIR/deploy/nginx.conf" ]; then
  if command -v envsubst >/dev/null 2>&1; then
    export DOMAIN
    envsubst '${DOMAIN}' < "$APP_DIR/deploy/nginx.conf" | sudo tee /etc/nginx/sites-available/empyrean >/dev/null
    sudo ln -sf /etc/nginx/sites-available/empyrean /etc/nginx/sites-enabled/empyrean
    echo "[deploy] Nginx configuration updated for $DOMAIN"
  fi
fi

sudo nginx -t && sudo systemctl restart nginx
REMOTE

# --- Production .env (never clobber existing secrets) ---
ssh "$SERVER_USER@$SERVER_HOST" bash -s -- "$APP_DIR" <<'REMOTE'
set -e
APP_DIR="$1"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/deploy/.env.production.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "[deploy] created $APP_DIR/.env from template — fill in SECRET_KEY, JWT_SECRET, MQTT_BROKER_HOST, DB password"
else
  echo "[deploy] $APP_DIR/.env already exists — left untouched"
fi
REMOTE

# --- Stop services → migrate → start (M72 + L50) ---
# The old order enabled/started the units BEFORE migrating, so a failed
# migration left new code serving on a half-applied schema — and already-
# running services were never restarted, so "deployed" code was not loaded.
# Now: stop whatever is running, apply the schema, then (re)start everything
# on the new code. A migration failure leaves the old state stopped, not
# half-migrated-and-serving.
ssh "$SERVER_USER@$SERVER_HOST" bash -s -- "$APP_DIR" <<'REMOTE'
set -e
APP_DIR="$1"

sudo systemctl enable quart-api celery-worker celery-beat
sudo systemctl stop quart-api celery-worker celery-beat || true

cd "$APP_DIR"
"$APP_DIR/venv/bin/alembic" upgrade head

sudo systemctl restart quart-api celery-worker celery-beat
REMOTE

echo "[deploy] SUCCESS — code, deps, migrations, and systemd services are live"
