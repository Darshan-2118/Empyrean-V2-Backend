@echo off
REM Stop the dev stack started by dev-up.bat (processes + WSL Redis).
setlocal

echo Stopping Empyrean dev services...

REM Kill by window titles
taskkill /FI "WINDOWTITLE eq empyrean-api*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq empyrean-celery-worker*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq empyrean-celery-beat*" /T /F >nul 2>nul

REM Kill lingering python/hypercorn/celery dev processes
taskkill /IM hypercorn.exe /F >nul 2>nul
taskkill /IM celery.exe /F >nul 2>nul

REM Shutdown Redis in WSL if running
wsl redis-cli ping >nul 2>&1
if %errorlevel% equ 0 (
    echo Stopping WSL Redis server...
    wsl redis-cli shutdown >nul 2>&1
)

echo.
echo All dev services and Redis stopped.
endlocal
