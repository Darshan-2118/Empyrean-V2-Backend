@echo off
REM WSL Instance keep-alive tab.
REM This attached wsl.exe process pins the WSL VM open for as long as this
REM window exists - without it, WSL tears down the whole VM on its idle
REM timeout, killing Redis (and every connection to it) mid-session.
title empyrean-wsl-instance
echo ================================================================
echo   WSL INSTANCE - DO NOT CLOSE THIS WINDOW
echo.
echo   Redis-server depends on this terminal to stay alive.
echo   Closing it will crash the dev stack.
echo   Use scripts\stop.bat to shut down instead.
echo ================================================================
wsl -e sh -c "exec sleep infinity"
