# Empyrean — Getting Started

This guide walks through setting up the Empyrean backend locally and running it in production. It covers prerequisites, installation, health checks, database migrations, seed data, and the systemd process layout.

## Prerequisites
- **Python** 3.12+
- **PostgreSQL** 17+ (18 recommended)
- **Redis** (for Celery broker + cache)

## Setup

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd Empyrean-V2-Backend

# 2. Create and activate virtual environment
python -m venv venv
source venv/Scripts/activate    # Windows Git Bash
# or: venv\Scripts\activate     # Windows cmd
# or: source venv/bin/activate  # Linux/Mac

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
#    Copy .env.example → .env and fill in your credentials:
#    DATABASE_URL=postgresql://postgres:your_password@localhost:5432/Empyrean
cp .env.example .env
# Edit .env with your actual database credentials

# 5. Create the database (if it doesn't exist)
psql -U postgres -c "CREATE DATABASE \"Empyrean\";"

# 6. Run migrations
alembic upgrade head

# 7. Seed initial data (admin user, defaults, sample node)
python scripts/seed.py

# 8. Verify everything is working
python scripts/check_health.py

# 9. Start the API server
hypercorn app:app --bind 0.0.0.0:8000

# (in separate terminals) Start Celery worker and beat
celery -A celery_app worker --loglevel=info
celery -A celery_app beat --loglevel=info
```

The API will be available at `http://localhost:8000/api/v1/`.

## Health Check

Run `python scripts/check_health.py` at any time to verify:
- Python environment and model imports
- PostgreSQL connection and database version
- All 7 tables and required indexes exist
- Alembic migration is applied
- Seed data is present (admin user, default settings, sample node)
- Quart app factory loads without errors

## Full Verification

Run a single command (any shell) to check everything at once before committing:

```bash
scripts\check                # cmd.exe / PowerShell
python scripts/verify.py     # Git Bash / Linux / macOS
python scripts/verify.py --quick  # skip tests, env only
```

Checks PostgreSQL, Alembic migrations, health check, and pytest all in one go.

## Database Migrations

```bash
# Create a new migration after model changes
alembic revision --autogenerate -m "description_of_change"

# Apply pending migrations
alembic upgrade head

# Roll back one step
alembic downgrade -1
```

## Seed Data

The `seed.py` script is idempotent — running it multiple times is safe:

```
Created admin user: admin / admin123
Created setting: aqi_warning_threshold = 100
Created setting: aqi_critical_threshold = 150
Created setting: alerts_enabled = true
Created sample node: ESP32-01
```

Default admin credentials: `admin` / `admin123` (change in production).

## Production

Run the same processes under `systemd` unit files (or `supervisord`) so they restart automatically and start on boot. A minimal `quart-api.service` example:

```ini
[Unit]
Description=Empyrean Quart API
After=network.target postgresql.service redis-server.service mosquitto.service

[Service]
WorkingDirectory=/opt/empyrean-backend
EnvironmentFile=/opt/empyrean-backend/.env
ExecStart=/opt/empyrean-backend/venv/bin/hypercorn app:app --bind 0.0.0.0:8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Mirror this pattern for `celery-worker.service` and `celery-beat.service`. Put TLS termination for the REST API (HTTPS) in front via nginx or Caddy on the same host.

## Related Docs

- [configuration.md](configuration.md)
- [project-structure.md](project-structure.md)
- [api.md](api.md)
- [README](../README.md)
