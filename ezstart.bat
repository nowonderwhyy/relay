@echo off
setlocal
cd /d "%~dp0"
title Relay

set "PORT=%RELAY_PORT%"
if "%PORT%"=="" set "PORT=8080"

echo.
echo  ========================================
echo    Relay - Easy Start
echo  ========================================
echo.

rem 1. Check if Relay is already running on this port
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%PORT%/healthz' -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :already_running

rem 2. Ensure aria2c is installed
where aria2c >nul 2>&1
if errorlevel 1 goto :missing_aria2

rem 3. Bootstrap virtual environment on first run
if exist ".venv\Scripts\relay.exe" goto :launch

echo [1/2] First-time setup: creating local environment...
where uv >nul 2>&1
if not errorlevel 1 goto :setup_uv

where py >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv .venv
    if errorlevel 1 goto :setup_failed
    goto :install_pip
)

where python >nul 2>&1
if errorlevel 1 goto :missing_python
python -m venv .venv
if errorlevel 1 goto :setup_failed
goto :install_pip

:setup_uv
uv venv .venv
if errorlevel 1 goto :setup_failed
echo [2/2] Installing Relay...
uv pip install --python ".venv\Scripts\python.exe" -e .
if errorlevel 1 goto :setup_failed
goto :launch

:install_pip
echo [2/2] Installing Relay...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
".venv\Scripts\pip.exe" install -e .
if errorlevel 1 goto :setup_failed
goto :launch

:launch
echo Starting Relay on http://127.0.0.1:%PORT%/
echo Keep this window open while downloading. Close it or press Ctrl+C to stop.
echo.
if "%RELAY_NO_OPEN%"=="1" (
    ".venv\Scripts\relay.exe" serve --portable --port "%PORT%" %*
) else (
    ".venv\Scripts\relay.exe" serve --portable --port "%PORT%" --open %*
)
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Relay stopped with exit code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%

:already_running
echo Relay is already running at http://127.0.0.1:%PORT%/
if not "%RELAY_NO_OPEN%"=="1" start "" "http://127.0.0.1:%PORT%/"
timeout /t 3 >nul
exit /b 0

:missing_aria2
echo [!] aria2c was not found on PATH.
where winget >nul 2>&1
if errorlevel 1 (
    echo     Install aria2 from https://aria2.github.io/ and re-run ezstart.bat.
    echo.
    pause
    exit /b 1
)
set /p "INSTALL_ARIA2=Install aria2 now via winget? [Y/n]: "
if /i "%INSTALL_ARIA2%"=="n" exit /b 1
winget install --id aria2.aria2 -e --accept-source-agreements --accept-package-agreements
echo.
echo Installed aria2. Close this window and double-click ezstart.bat again so the new PATH takes effect.
pause
exit /b 0

:missing_python
echo [!] Python 3.10+ was not found on PATH.
echo     Install Python from https://www.python.org/downloads/ (check "Add Python to PATH")
echo     or install uv (winget install astral-sh.uv), then double-click ezstart.bat again.
echo.
pause
exit /b 1

:setup_failed
echo.
echo [!] Setup failed. Check the messages above and try again.
pause
exit /b 1
