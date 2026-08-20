@echo off
REM Stop the dev stack started by dev-up.bat (best-effort, by window title).
setlocal

taskkill /FI "WINDOWTITLE eq empyrean-api*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq empyrean-celery-worker*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq empyrean-celery-beat*" /T /F >nul 2>nul

echo Stopped empyrean-api / empyrean-celery-worker / empyrean-celery-beat windows.
echo (A Redis you started in WSL is not touched — stop it with: wsl redis-cli shutdown)
endlocal
