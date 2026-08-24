@echo off
REM Dev stack: Celery worker + Celery beat + HTTP API (Redis started separately).
REM On Windows, beat must run as its own process (-B is not supported on Windows).
REM Each component runs in its own console window so logs are visible.
setlocal
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

REM Check and auto-start Redis via WSL
echo Checking Redis connectivity...
wsl redis-cli ping >nul 2>&1
if %errorlevel% neq 0 (
    echo Starting Redis in WSL...
    wsl redis-server --daemonize yes >nul 2>&1
    timeout /t 1 /nobreak >nul 2>&1
    wsl redis-cli ping >nul 2>&1
    if %errorlevel% neq 0 (
        echo ERROR: Failed to start Redis in WSL.
        echo Please ensure WSL is installed and Redis is available: wsl redis-server --daemonize yes
        exit /b 100
    )
)
echo Redis is running.
echo.

REM --- Launch services (Windows Terminal multi-tab with fallback) ---
where wt >nul 2>&1
if %errorlevel% equ 0 (
    echo Launching Celery Worker, Beat, and API in a single Windows Terminal with tabs...
    wt -d "%CD%" --title "Celery Worker" cmd /k "title empyrean-celery-worker && .venv\Scripts\celery.exe -A celery_app.celery_app worker --loglevel=info" ; new-tab -d "%CD%" --title "Celery Beat" cmd /k "title empyrean-celery-beat && .venv\Scripts\celery.exe -A celery_app.celery_app beat --loglevel=info" ; new-tab -d "%CD%" --title "HTTP API" cmd /k "title empyrean-api && .venv\Scripts\hypercorn.exe ""app:create_app()"" --bind 0.0.0.0:8000"
) else (
    echo Windows Terminal (wt.exe) not found; launching in separate windows...
    start "empyrean-celery-worker" cmd /k .venv\Scripts\celery.exe -A celery_app.celery_app worker --loglevel=info
    start "empyrean-celery-beat" cmd /k .venv\Scripts\celery.exe -A celery_app.celery_app beat --loglevel=info
    start "empyrean-api" cmd /k .venv\Scripts\hypercorn.exe "app:create_app()" --bind 0.0.0.0:8000
)

echo.
echo Dev stack launched successfully.
echo API listening on http://localhost:8000
echo Check GET http://localhost:8000/api/v1/admin/health for component status.
endlocal