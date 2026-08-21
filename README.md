# Empyrean-V2-Backend

## How to Run the Project

### Prerequisites
- Python 3.10–3.12
- `git` installed and the repo cloned
- Redis running (the scripts below assume you start it via WSL on Windows)
- PostgreSQL pointed at by `DATABASE_URL` in `.env`

### 1. Create & activate a virtual environment
```bash
# Windows
python -m venv .venv
.\.venv\Scripts\activate

# Linux / macOS
python -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Set up environment variables
```bash
cp .env.example .env
```
Open `.env` and adjust values you need (`DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`, etc.). If you run Redis locally via WSL, the default `REDIS_URL=redis://localhost:6379/0` will work.

> ⚠️ **Security note:** `SECRET_KEY` and `JWT_SECRET` in `.env.example` are placeholders only. Never keep them as-is or use weak/guessable values (e.g. your name). Generate strong random secrets before running the app:
> ```bash
> python -c "import secrets; print(secrets.token_hex(32))"
> ```
> Run this twice — once for `SECRET_KEY`, once for `JWT_SECRET` — and use each output as the real value in your local `.env`. Never commit `.env` or put real secrets in `.env.example`.

### 4. Start Redis (via WSL on Windows)
```bash
wsl redis-server --daemonize yes
```

### 5. Apply database migrations (first time only)
```bash
alembic upgrade head
```

### 6. Seed the database (optional, first run)
```bash
python scripts/seed.py
```

### 7. Start the services

**Option A — Quick Start (recommended):**
```bash
scripts\dev-up.bat
```
This launches three separate console windows:
- ✅ Celery worker + beat scheduler
- ✅ API server (Hypercorn on port 8000)
- ✅ Redis connectivity check

**Option B — Manual component startup:**

Terminal 1 — Celery worker & beat:
```bash
celery -A celery_app.celery_app worker -B --loglevel=info
```

Terminal 2 — API server:
```bash
hypercorn app:app --bind 0.0.0.0:8000
```

> On Windows, beat must run as its own process (`-B` is not supported in the same process).

### 8. Stop the dev stack
```bash
scripts\dev-down.bat
```

---

## ✅ How to Test the Codebase

Make sure the prerequisites are in place first:
1. Virtual environment activated — `.\.venv\Scripts\activate`
2. Dependencies installed — `pip install -r requirements.txt`
3. Redis running — `wsl redis-server --daemonize yes`
4. Database migrated — `alembic upgrade head`
5. `.env` file configured (copied from `.env.example`)

### 1. Run the test suite (quickest)
```bash
pytest
```

### 2. Start the full stack & health check
```bash
# Start everything (opens separate windows for Celery worker, beat, and API server)
scripts\dev-up.bat

# Verify it's working
curl http://localhost:8000/admin/health
```
You should get a JSON response reporting the health of the API, Celery, Redis, and MQTT.

### 3. Manual component startup (if you want more control)

Terminal 1 — Celery worker & beat:
```bash
celery -A celery_app.celery_app worker -B --loglevel=info
```

Terminal 2 — API server:
```bash
hypercorn app:app --bind 0.0.0.0:8000
```

Then run the health check:
```bash
curl http://localhost:8000/admin/health
```

The health check endpoint (`/admin/health`) is the quickest way to verify everything is wired up correctly end-to-end.

---

## 🛑 Stopping Redis and Celery

### Stopping Redis
The project uses Redis started inside the Windows Subsystem for Linux (WSL). To shut it down cleanly:
```bash
# Inside the WSL terminal where Redis is running
wsl sudo systemctl stop redis-server
```

### Stopping Celery
Celery runs as a separate background process (or as part of the `celery -A celery_app.celery_app worker -B` command).

- **If you started Celery via a terminal window** (e.g., using `dev-up.bat` or manually), simply press **Ctrl + C** in that terminal. This sends a termination signal that stops the worker and the beat scheduler.
- **If you need to stop it from another terminal**, terminate the Celery process by name:
  - **Linux/macOS**:
    ```bash
    pkill -f celery
    ```
  - **Windows PowerShell**:
    ```powershell
    Get-Process -Name celery | Stop-Process -Force
    ```
  - Alternatively, open Task Manager (or `htop` on Linux) and kill the `celery` process directly.

> **Tip:** Always shut down Redis and Celery before restarting the whole stack to avoid stale socket connections or "address already in use" errors.

---

## ⚙️ Development Tools

- Run individual components:
  - `celery -A celery_app.celery_app worker --loglevel=info`
  - `hypercorn app:app --bind 0.0.0.0:8000`
- Health check: `curl http://localhost:8000/admin/health`
- Stop the stack: `scripts\dev-down.bat`
- Run tests: `pytest`

---

## 🗂️ Project Structure

Key folders:
- `app/` — API
- `tasks/` — Celery tasks
- `mqtt/` — MQTT integration
- `config/` — Settings
- `scripts/` — Dev tools

---

## 📦 Quick Reference

| Action | Command |
|--------|---------|
| Create venv | `python -m venv .venv` |
| Activate venv | `.\.venv\Scripts\activate` |
| Install deps | `pip install -r requirements.txt` |
| Copy env file | `cp .env.example .env` |
| Start Redis (WSL) | `wsl redis-server --daemonize yes` |
| Migrate DB | `alembic upgrade head` |
| Seed DB | `python scripts/seed.py` |
| Start dev stack | `scripts\dev-up.bat` |
| Start dev stack (single console) | `scripts\dev-up-quick.bat` |
| Stop dev stack | `scripts\dev-down.bat` |
| Run tests | `pytest` |
| Health check | `curl http://localhost:8000/admin/health` |
| Stop Redis (WSL) | `wsl sudo systemctl stop redis-server` |
| Stop Celery (Linux/macOS) | `pkill -f celery` |
| Stop Celery (Windows) | `Get-Process -Name celery \| Stop-Process -Force` |

---

## 💡 Gotchas & Tips

- **Redis** — The repo expects Redis reachable via WSL on Windows. On native Windows, install Redis for Windows or run it in Docker and point `REDIS_URL` accordingly.
- **MQTT** — Broker settings live in `.env`. Without a broker, comment out the MQTT lifecycle registration in `app_factory/factory.py` or set `MQTT_BROKER_HOST=localhost` / `MQTT_BROKER_PORT=1883` (default Mosquitto port).
- **Celery beat** — On Windows, `-B` (beat in the same process) is not supported, so the batch script starts beat in its own window.
- **Debugging** — If you hit a `ModuleNotFoundError`, double-check the virtual environment is activated and `pip install -r requirements.txt` succeeded.