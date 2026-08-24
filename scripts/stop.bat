@echo off
REM Stop the dev stack started by start.bat (processes + WSL Redis).
setlocal

echo Stopping Empyrean dev services...

REM Kill by window titles
taskkill /FI "WINDOWTITLE eq empyrean-server*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq empyrean-celery-worker*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq empyrean-celery-beat*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq empyrean-wsl-instance*" /T /F >nul 2>nul

REM Kill any leftover keep-alive sleep process inside WSL
wsl -e sh -c "pkill -f 'sleep infinity' >/dev/null 2>&1" >nul 2>&1

REM Kill lingering python/hypercorn/celery dev processes
taskkill /IM hypercorn.exe /F >nul 2>nul
taskkill /IM celery.exe /F >nul 2>nul

REM Shutdown Redis in WSL if running
wsl redis-cli ping >nul 2>&1
if %errorlevel% equ 0 (
    echo Stopping WSL Redis server...
    REM Use systemctl stop, not `redis-cli shutdown`: the service has
    REM Restart=always, so redis-cli shutdown just gets auto-restarted.
    REM Passwordless via /etc/sudoers.d/empyrean-redis (scoped to redis only).
    wsl sudo -n /usr/bin/systemctl stop redis-server.service >nul 2>&1
)

echo.
echo All dev services and Redis stopped.
endlocal
