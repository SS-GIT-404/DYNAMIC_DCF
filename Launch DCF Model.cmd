@echo off
REM ===================================================================
REM  Dynamic DCF Valuation Model - local launcher
REM
REM  Double-click this file to start the tool. It runs entirely on this
REM  computer: the server binds to 127.0.0.1 (loopback), so nothing is
REM  reachable from any other device on the network.
REM
REM  Close this black window to shut the tool down.
REM ===================================================================

setlocal
cd /d "%~dp0"
title Dynamic DCF Valuation Model

echo.
echo   Dynamic DCF Valuation Model
echo   ---------------------------------------------
echo   Starting on this computer only (127.0.0.1)
echo.

REM --- locate the virtual environment ------------------------------
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo   First run detected - setting up. This takes a few minutes.
    echo.

    REM Windows intercepts bare "python" with a Microsoft Store stub, so
    REM prefer the py launcher, which the real installer provides.
    set "BOOTSTRAP="
    py -3 --version >nul 2>&1 && set "BOOTSTRAP=py -3"
    if not defined BOOTSTRAP (
        python --version >nul 2>&1 && set "BOOTSTRAP=python"
    )
    if not defined BOOTSTRAP (
        echo   [X] Python was not found on this computer.
        echo.
        echo       Install Python 3.9 or newer from:
        echo         https://www.python.org/downloads/
        echo       Tick "Add python.exe to PATH" during installation,
        echo       then double-click this file again.
        echo.
        pause
        exit /b 1
    )

    echo   Creating the environment...
    %BOOTSTRAP% -m venv ".venv"
    if errorlevel 1 goto setup_failed

    echo   Installing components...
    "%VENV_PY%" -m pip install --upgrade pip --quiet
    "%VENV_PY%" -m pip install -r requirements.txt --quiet
    if errorlevel 1 goto setup_failed

    echo   Setup complete.
    echo.
)

REM --- check the SEC contact address is configured ------------------
if not exist "%~dp0settings.local.env" (
    echo   [!] settings.local.env is missing.
    echo       The SEC blocks requests that do not identify the caller.
    echo       Create the file next to this launcher containing:
    echo.
    echo         SEC_UA_EMAIL=your.address@example.com
    echo.
    pause
    exit /b 1
)

REM --- open the browser once the server is actually accepting -------
start "" /b powershell -NoProfile -WindowStyle Hidden -Command ^
  "for ($i=0; $i -lt 60; $i++) { try { Invoke-WebRequest -Uri 'http://localhost:8501' -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process 'http://localhost:8501'; break } catch { Start-Sleep -Milliseconds 500 } }"

echo   Opening in your browser: http://localhost:8501
echo.
echo   Leave this window open while you use the tool.
echo   Close it to shut down.
echo   ---------------------------------------------
echo.

"%VENV_PY%" -m streamlit run app.py

echo.
echo   Tool stopped.
timeout /t 3 >nul
exit /b 0

:setup_failed
echo.
echo   [X] Setup failed. Check your internet connection and try again.
echo.
pause
exit /b 1
