# Empyrean — Getting Started

This guide walks through setting up the Empyrean backend locally and running it in production. It covers prerequisites, installation, health checks, database migrations, seed data, and the systemd process layout.

## Prerequisites

- **Python** 3.12+
- **PostgreSQL** 17+ (18 recommended)
- **Redis** (for Celery broker + cache + rate limiting)

## Quick Start (Recommended for Development)

The easiest way to get everything running is to use the provided startup scripts:

### Option 1: Using the Development Scripts (Recommended)

1. **Clone and enter the repo**
   ```bash
   git clone <repo-url>
   cd Empyrean-V2-Backend
   ```

2. **Create and activate virtual environment**
   ```bash
   python -m venv venv
   source venv/Scripts/activate    # Windows Git Bash
   # or: venv\Scripts\activate     # Windows cmd
   # or: source venv/bin/activate  # Linux/Mac
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment & secrets**
   ```bash
   cp .env.example .env
   # Automatically generate cryptographically strong secrets for .env:
   python scripts/generate_secrets.py --write-env
   # Edit .env with your actual database credentials
   # Default: DATABASE_URL=postgresql://postgres:your_password@localhost:5432/Empyrean
   ```

5. **Create the database** (if it doesn't exist)
   ```bash
   psql -U postgres -c "CREATE DATABASE \"Empyrean\";"
   ```
   > 📖 For comprehensive PostgreSQL + TimescaleDB installation across Docker, Windows, Linux, macOS, and Cloud, see [database-setup.md](database-setup.md).  
   > 🎥 Need a visual installation walkthrough? Check out this [TimescaleDB Installation Video Tutorial (YouTube)](https://youtu.be/KlOGfFzLdqA).

6. **Run migrations**
   ```bash
   alembic upgrade head
   ```

7. **Seed initial data** (admin user, defaults, sample node)
   ```bash
   python scripts/seed.py
   ```
   > 💡 **Admin User:**
   > There are no hardcoded credentials. Set `BOOTSTRAP_ADMIN_USERNAME` and
   > `BOOTSTRAP_ADMIN_PASSWORD` (optionally `BOOTSTRAP_ADMIN_EMAIL`) in `.env`
   > before seeding — the seeder creates that user with the `admin` role.

8. **Pre-flight health check**
   ```bash
   python scripts/check_health.py
   ```
   > ℹ️ **Note on Redis Connectivity:**
   > If Redis is not yet running, the Redis check will report `[FAIL]`. This is normal when using `scripts\start.bat`, which automatically provisions Redis in WSL at runtime. You can either start Redis manually (`wsl sudo -n /usr/sbin/service redis-server start`) before running the health check or re-run `python scripts/check_health.py` once the stack is running.

9. **Start all services** with one command (auto-starts Redis & multi-tab terminal on Windows):
   ```bash
   scripts\start.bat  # Windows
   # or for Linux/Mac:
   ./scripts/start.sh # (if you create one)
   ```

   The `start.bat` script will:
   - Check and automatically start Redis in WSL (as a systemd service, passwordless via a scoped sudoers rule) if not running
   - Wait until Redis answers PING (up to 5 s) and abort without launching anything if it never becomes ready
   - Launch **WSL Instance** (a keep-alive that pins the WSL VM open so Redis isn't torn down mid-session), **Celery worker**, **Celery beat**, and **Empyrean Server** inside Windows Terminal tabs
   - Keep periodic schedule files clean inside the `.celery/` directory

   To stop all services and Redis:
   ```bash
   scripts\stop.bat
   ```

### Option 2: Manual Process Management

If you prefer to manage each process individually:

1. **Start Redis** (keep this running in a terminal)
   ```bash
   wsl redis-server --daemonize yes  # WSL2
   # or your native Redis command
   ```

2. **Start Celery worker** (separate terminal)
   ```bash
   celery -A celery_app.celery_app worker --loglevel=info
   ```

3. **Start Celery beat** (separate terminal)
   ```bash
   celery -A celery_app.celery_app beat --loglevel=info
   ```

4. **Start HTTP API** (separate terminal)
   ```bash
   hypercorn "app:create_app()" --bind 0.0.0.0:8000
   ```

## Verification

After starting all services, verify everything is working:

```bash
# Liveness check (no auth, always 200 while the API is up)
curl http://localhost:8000/health

# Full component health (requires an admin JWT; fail-soft — always 200,
# per-component status in the JSON body)
curl -H "Authorization: Bearer <admin_access_token>" http://localhost:8000/api/v1/admin/health

# Or use the health check script
python scripts/check_health.py
```

The API will be available at `http://localhost:8000/api/v1/`.

## Required Process Set

For the system to function fully, you need these **four** processes:

| Process | Purpose | How to Start |
|---------|---------|--------------|
| **HTTP API** | request handling | `hypercorn "app:create_app()" --bind 0.0.0.0:8000` |
| **Redis** | cache + rate-limit + Celery broker | `wsl redis-server --daemonize yes` (WSL) or native Redis |
| **Celery worker** | `process_reading`, alerts, aggregation, forecast | `celery -A celery_app.celery_app worker --loglevel=info` |
| **Celery beat** | schedules (60s/1h/1d periodic tasks) | `celery -A celery_app.celery_app beat --loglevel=info` |

## Production Deployment

Production unit files, nginx config, and the deploy script live in [`deploy/`](../deploy/) — use them instead of hand-rolling units:

| File | Purpose |
|------|---------|
| `deploy/quart-api.service` | Hypercorn API under an unprivileged `empyrean` user |
| `deploy/celery-worker.service` | Celery worker (same hardening) |
| `deploy/celery-beat.service` | Celery beat with a `flock` single-instance guard and `RuntimeDirectory=empyrean` |
| `deploy/nginx.conf` | TLS termination, `/api/v1/export` 330s proxy timeout, `/metrics` locked to localhost |
| `deploy/deploy.sh` | Idempotent rsync + migrations + service restarts (excludes `.venv`/`.celery`) |
| `deploy/.env.production.example` | Production environment template |

All three units run as `User=empyrean`, restart with bounded retry (`StartLimitIntervalSec=300`, `StartLimitBurst=5`), and read their environment from `/opt/empyrean/.env`.

Put TLS termination for the REST API (HTTPS) in front via nginx or Caddy on the same host.

## Stopping Services

To stop the development stack:

```bash
scripts\stop.bat  # Windows
# Stops: Server, Celery worker, Celery beat, and WSL Instance windows,
# then stops the Redis systemd service inside WSL.
```

For manual processes, use Ctrl+C in each terminal window.

## Admin Credentials

There is no built-in default account. Provision the admin via environment variables **before** running `scripts/seed.py`:

```bash
BOOTSTRAP_ADMIN_USERNAME=youradmin
BOOTSTRAP_ADMIN_PASSWORD=<strong password>   # ≥ 8 chars, mixed case, digit, symbol
BOOTSTRAP_ADMIN_EMAIL=ops@example.com        # optional
```

The seeder creates (or promotes) that user with the `admin` role. The password must pass the strength gate and is never a known-weak value. **Rotate it via `/api/v1/profile/change-password` after first login.**

## Troubleshooting

### Common Issues

1. **Redis connection failed**
   - Ensure Redis is running: `wsl redis-cli ping` should return `PONG`
   - Check WSL2 is installed and running: `wsl --list --verbose`

2. **Celery worker not processing tasks**
   - Verify Redis is running and accessible
   - Check worker logs for error messages
   - Ensure you started the worker with the correct app: `celery -A celery_app.celery_app worker`

3. **Database connection failed**
   - Verify PostgreSQL is running: `psql -U postgres -l`
   - Check `.env` file has correct `DATABASE_URL`
   - Ensure the Empyrean database was created

4. **Missing Python dependencies**
   - Re-run: `pip install -r requirements.txt`
   - Consider updating pip: `python -m pip install --upgrade pip`

### Health Check Endpoints

- **Liveness check** (no auth, always 200 if API is up): `GET /health`
- **Component health** (admin JWT required; fail-soft — always 200, per-component status in the body): `GET /api/v1/admin/health`
- **Component details**: check the JSON body for individual service status

## Next Steps

Once everything is running:
1. Read the API reference in [api.md](api.md)
2. Try creating a sensor reading via MQTT or the test endpoints
3. Monitor the system via `GET /api/v1/admin/health` (admin JWT)
4. Check Celery task processing in the worker logs
5. Review generated forecasts with `GET /api/v1/forecast?node_id=<node_id>`

For production deployment, consider:
- Setting strong secrets in `.env` (JWT_SECRET, SECRET_KEY)
- Configuring proper SSL/TLS termination
- Setting up log rotation and monitoring
- Configuring alerts for system health degradation