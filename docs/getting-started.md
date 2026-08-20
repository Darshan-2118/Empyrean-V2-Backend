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

4. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your actual database credentials
   # Default: DATABASE_URL=postgresql://postgres:your_password@localhost:5432/Empyrean
   ```

5. **Create the database** (if it doesn't exist)
   ```bash
   psql -U postgres -c "CREATE DATABASE \"Empyrean\";"
   ```

6. **Run migrations**
   ```bash
   alembic upgrade head
   ```

7. **Seed initial data** (admin user, defaults, sample node)
   ```bash
   python scripts/seed.py
   ```

8. **Start Redis** (required for Celery and caching)
   ```bash
   # On Windows with WSL2:
   wsl redis-server --daemonize yes
   
   # Alternative: Install Redis natively for your OS
   # Ubuntu/Debian: sudo apt-get install redis-server && sudo systemctl start redis-server
   # macOS: brew install redis && brew services start redis
   # Windows: Download from https://redis.io/download and run redis-server.exe
   ```

9. **Start all services** with one command (opens separate windows):
   ```bash
   scripts\dev-up.bat  # Windows
   # or for Linux/Mac:
   ./scripts/dev-up.sh # (if you create one)
   ```

   The `dev-up.bat` script will start:
   - **Celery worker** - processes sensor readings, alerts, aggregations, forecasts
   - **Celery beat** - schedules periodic tasks (health checks, model retraining, cleanup)
   - **HTTP API** - serves the REST API on http://localhost:8000

   You must start Redis separately first (step 8).

### Option 2: Manual Process Management

If you prefer to manage each process individually:

1. **Start Redis** (keep this running in a terminal)
   ```bash
   wsl redis-server --daemonize yes  # WSL2
   # or your native Redis command
   ```

2. **Start Celery worker** (separate terminal)
   ```bash
   celery -A celery_app worker --loglevel=info
   ```

3. **Start Celery beat** (separate terminal)
   ```bash
   celery -A celery_app beat --loglevel=info
   ```

4. **Start HTTP API** (separate terminal)
   ```bash
   hypercorn app:app --bind 0.0.0.0:8000
   ```

## Verification

After starting all services, verify everything is working:

```bash
# Check health of all components
curl http://localhost:8000/admin/health

# For detailed health with strict mode (returns 503 if any critical component is down)
curl http://localhost:8000/admin/health?strict=1

# Or use the health check script
python scripts/check_health.py
```

The API will be available at `http://localhost:8000/api/v1/`.

## Required Process Set

For the system to function fully, you need these **four** processes:

| Process | Purpose | How to Start |
|---------|---------|--------------|
| **HTTP API** | request handling | `hypercorn app:app --bind 0.0.0.0:8000` |
| **Redis** | cache + rate-limit + Celery broker/backend | `wsl redis-server --daemonize yes` (WSL) or native Redis |
| **Celery worker** | `process_reading`, alerts, aggregation, forecast | `celery -A celery_app worker --loglevel=info` |
| **Celery beat** | schedules (60s/1h/1d periodic tasks) | `celery -A celery_app beat --loglevel=info` |

## Production Deployment

Run the same processes under `systemd` unit files (or `supervisord`) so they restart automatically and start on boot.

### Example systemd services:

**quart-api.service**
```ini
[Unit]
Description=Empyrean Quart API
After=network.target postgresql.service redis-server.service

[Service]
WorkingDirectory=/opt/empyrean-backend
EnvironmentFile=/opt/empyrean-backend/.env
ExecStart=/opt/empyrean-backend/venv/bin/hypercorn app:app --bind 0.0.0.0:8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

**celery-worker.service**
```ini
[Unit]
Description=Empyrean Celery Worker
After=network.target postgresql.service redis-server.service

[Service]
WorkingDirectory=/opt/empyrean-backend
EnvironmentFile=/opt/empyrean-backend/.env
ExecStart=/opt/empyrean-backend/venv/bin/celery -A celery_app worker --loglevel=info
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

**celery-beat.service**
```ini
[Unit]
Description=Empyrean Celery Beat
After=network.target postgresql.service redis-server.service

[Service]
WorkingDirectory=/opt/empyrean-backend
EnvironmentFile=/opt/empyrean-backend/.env
ExecStart=/opt/empyrean-backend/venv/bin/celery -A celery_app beat --loglevel=info
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Put TLS termination for the REST API (HTTPS) in front via nginx or Caddy on the same host.

## Stopping Services

To stop the development stack:

```bash
scripts\dev-down.bat  # Windows
# Stops: API, Celery worker, Celery beat windows
# Note: You must manually stop Redis: wsl redis-cli shutdown
```

For manual processes, use Ctrl+C in each terminal window.

## Default Admin Credentials

After seeding, the default admin user is:
- **Username**: admin
- **Password**: admin123

**Change this password immediately in production!**

## Troubleshooting

### Common Issues

1. **Redis connection failed**
   - Ensure Redis is running: `wsl redis-cli ping` should return `PONG`
   - Check WSL2 is installed and running: `wsl --list --verbose`

2. **Celery worker not processing tasks**
   - Verify Redis is running and accessible
   - Check worker logs for error messages
   - Ensure you started the worker with the correct app: `celery -A celery_app worker`

3. **Database connection failed**
   - Verify PostgreSQL is running: `psql -U postgres -l`
   - Check `.env` file has correct `DATABASE_URL`
   - Ensure the Empyrean database was created

4. **Missing Python dependencies**
   - Re-run: `pip install -r requirements.txt`
   - Consider updating pip: `python -m pip install --upgrade pip`

### Health Check Endpoints

- **Liveness check** (always 200 if API is up): `GET /admin/health`
- **Readiness check** (503 if critical deps down): `GET /admin/health?strict=1`
- **Component details**: Check the JSON body for individual service status

## Next Steps

Once everything is running:
1. Visit the API documentation at `http://localhost:8000/api/v1/` (if swagger is enabled)
2. Try creating a sensor reading via MQTT or the test endpoints
3. Monitor the system via `GET /admin/health?strict=1`
4. Check Celery task processing in the worker logs
5. Review generated forecasts with `GET /forecast/{node_id}`

For production deployment, consider:
- Setting strong secrets in `.env` (JWT_SECRET, SECRET_KEY)
- Configuring proper SSL/TLS termination
- Setting up log rotation and monitoring
- Configuring alerts for system health degradation