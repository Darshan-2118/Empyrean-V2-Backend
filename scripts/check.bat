@echo off
REM Empyrean — check.bat
REM Works in cmd.exe and PowerShell. Calls the Python verify script.
REM
REM Usage:
REM   check            quick checks only (no pytest)
REM   check --full     full suite including pytest
REM ─────────────────────────────────────────────────────────────────────────────

python "%~dp0verify.py" %*
if %ERRORLEVEL% neq 0 (
    pause
    exit /b %ERRORLEVEL%
)
