@echo off
REM Dev stack: Celery worker + Celery beat + HTTP API (Redis started separately).
REM On Windows, beat must run as its own process (-B is not supported on Windows).
REM Each component runs in its own console window so logs are visible.
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

REM --- Prerequisite checks ---
echo Checking prerequisites...
if not exist ".venv" (
    echo ERROR: .venv directory not found.
    echo Run: python -m venv .venv
    exit /b 100
)
call .venv\Scripts\activate.bat

where hypercorn >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: hypercorn not found in PATH.
    echo Install dependencies: pip install -r requirements.txt
    exit /b 100
)

where celery >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: celery not found in PATH.
    echo Install dependencies: pip install -r requirements.txt
    exit /b 100
)

where alembic >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: alembic not found in PATH.
    echo Install dependencies: pip install -r requirements.txt
    exit /b 100
)

REM Check environment file and secrets
if not exist ".env" (
    echo .env not found. Initializing .env with secure cryptographic secrets...
    python scripts\generate_secrets.py --write-env
)

REM Check and auto-start Redis via WSL systemd service (daemonized, survives window closes)
echo Checking Redis connectivity...
wsl redis-cli ping >nul 2>&1
if !errorlevel! neq 0 (
    echo Starting Redis via WSL service...
    REM Passwordless via /etc/sudoers.d/empyrean-redis (scoped to redis-server
    REM only). NOTE: the .service suffix is required — sudoers matches the
    REM exact command string.
    wsl sudo -n /usr/sbin/service redis-server start >nul 2>&1
)

REM Wait up to 5 seconds (5 x 1s) for Redis to accept connections, then
REM require 2 consecutive successful PINGs — a single PING right after
REM `service start` can race the systemd start/restart cycle and the worker
REM would launch into a connection that is about to bounce.
REM The stack is NOT launched unless Redis is stably reachable.
set /a ATTEMPT=0
set /a OK_STREAK=0
:wait_redis
wsl redis-cli ping >nul 2>&1
if !errorlevel! equ 0 (
    set /a OK_STREAK+=1
    if !OK_STREAK! geq 2 goto :redis_ok
) else (
    set /a OK_STREAK=0
)
set /a ATTEMPT+=1
if !ATTEMPT! geq 5 (
    echo ERROR: Redis did not become ready within 5 seconds. Aborting.
    echo Fix manually with: wsl sudo -n /usr/sbin/service redis-server start
    exit /b 100
)
timeout /t 1 /nobreak >nul 2>&1
goto :wait_redis

:redis_ok
echo Redis is running.
echo.

REM --- Launch services (Windows Terminal multi-tab with fallback) ---
REM The first tab ("WSL Instance") runs `sleep infinity` inside WSL. Its
REM attached wsl.exe process pins the VM open for as long as the terminal
REM window exists — without it, WSL tears down the whole VM on its idle
REM timeout, killing Redis (and every connection to it) mid-session.
where wt >nul 2>&1
if %errorlevel% equ 0 goto :launch_wt

:launch_separate
echo Windows Terminal (wt.exe) not found; launching in separate windows...
start "empyrean-wsl-instance" cmd /k "scripts\wsl-instance.bat"
start "empyrean-tunnel" cmd /k "cd /d "%CD%" && title empyrean-tunnel && python scripts\banner.py tunnel && cloudflared tunnel run empyrean"
start "empyrean-celery-worker" cmd /k "cd /d "%CD%" && call .venv\Scripts\activate.bat && title empyrean-celery-worker && python scripts\banner.py worker && .venv\Scripts\celery.exe -A celery_app.celery_app worker --pool=solo --loglevel=info"
start "empyrean-celery-beat" cmd /k "cd /d "%CD%" && call .venv\Scripts\activate.bat && title empyrean-celery-beat && python scripts\banner.py beat && .venv\Scripts\celery.exe -A celery_app.celery_app beat --loglevel=info"
start "empyrean-server" cmd /k "cd /d "%CD%" && call .venv\Scripts\activate.bat && title empyrean-server && python scripts\banner.py server && .venv\Scripts\hypercorn.exe app:create_app() --bind 0.0.0.0:8000"
goto :done

:launch_wt
echo Launching WSL Instance, Tunnel, Celery Worker, Beat, and Server in a single Windows Terminal with tabs...
wt -d "%CD%" --title "WSL Instance" cmd /k "scripts\wsl-instance.bat" ; new-tab -d "%CD%" --title "Cloudflare Tunnel" cmd /k "title empyrean-tunnel && python scripts\banner.py tunnel && cloudflared tunnel run empyrean" ; new-tab -d "%CD%" --title "Celery Worker" cmd /k "call .venv\Scripts\activate.bat && title empyrean-celery-worker && python scripts\banner.py worker && .venv\Scripts\celery.exe -A celery_app.celery_app worker --pool=solo --loglevel=info" ; new-tab -d "%CD%" --title "Celery Beat" cmd /k "call .venv\Scripts\activate.bat && title empyrean-celery-beat && python scripts\banner.py beat && .venv\Scripts\celery.exe -A celery_app.celery_app beat --loglevel=info" ; new-tab -d "%CD%" --title "Empyrean Server" cmd /k "call .venv\Scripts\activate.bat && title empyrean-server && python scripts\banner.py server && .venv\Scripts\hypercorn.exe app:create_app() --bind 0.0.0.0:8000"

:done

echo.
echo Dev stack launched successfully.
echo API listening on http://localhost:8000
echo Check GET http://localhost:8000/api/v1/admin/health for component status.
endlocal