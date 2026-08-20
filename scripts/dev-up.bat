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

REM --- Celery worker ---
start "empyrean-celery-worker" cmd /k venv\Scripts\celery.exe -A celery_app.celery_app worker --loglevel=info

REM --- Celery beat (scheduler) ---
start "empyrean-celery-beat" cmd /k venv\Scripts\celery.exe -A celery_app.celery_app beat --loglevel=info

REM --- HTTP API ---
start "empyrean-api" cmd /k venv\Scripts\python.exe app.py

echo Dev stack launching in separate windows: worker / beat / api.
echo (Start Redis yourself: wsl redis-server --daemonize yes)
echo Check GET /admin/health for component status.
endlocal
