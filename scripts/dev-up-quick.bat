@echo off
REM Quick launcher for Celery worker+beat + API in separate windows.
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

REM Check Redis connectivity via WSL
echo Checking Redis connectivity...
wsl redis-cli ping >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Redis is not reachable via WSL.
    echo Start Redis with: wsl redis-server --daemonize yes
    exit /b 100
)
echo Redis is reachable.
echo.

REM --- Celery worker + beat (-B runs beat inside the worker for dev) ---
start "empyrean-celery" cmd /k .venv\Scripts\celery.exe -A celery_app.celery_app worker -B --loglevel=info

REM --- HTTP API ---
start "empyrean-api" cmd /k .venv\Scripts\hypercorn.exe "app:create_app()" --bind 0.0.0.0:8000

echo Launched empyrean-celery / empyrean-api windows.
echo Redis (WSL): wsl redis-server --daemonize yes  (already running if redis-cli ping = PONG)
endlocal