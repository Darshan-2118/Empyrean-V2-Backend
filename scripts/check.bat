@echo off
REM Empyrean — verify.bat
REM Works in cmd.exe and PowerShell. Calls the Python verify script.
REM
REM Usage:
REM   verify           full suite
REM   verify --quick   skip tests
REM ─────────────────────────────────────────────────────────────────────────────

python "%~dp0scripts\verify.py" %*
if %ERRORLEVEL% neq 0 (
    pause
    exit /b %ERRORLEVEL%
)
