@echo off
REM Creates a desktop shortcut to the DCF model launcher.
REM Run this once; afterwards you can start the tool from your desktop.

setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1"

if errorlevel 1 (
    echo.
    echo   [X] Could not create the shortcut.
    pause
    exit /b 1
)

pause
