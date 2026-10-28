@echo off
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

set "PY="
where python >nul 2>nul && set "PY=python"
if "%PY%"=="" (
  where py >nul 2>nul && set "PY=py -3"
)

if "%PY%"=="" (
  echo.
  echo [error] Python not found.
  echo Install Python 3.9+ and check "Add Python to PATH" during setup.
  echo Download: https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

if not exist "scripts\panel_server.py" (
  echo.
  echo [error] scripts\panel_server.py not found.
  echo Make sure start-panel.bat and the scripts folder are in the same skill directory.
  echo.
  pause
  exit /b 1
)

echo.
echo  Lingshu memory panel is starting...
echo  URL: http://127.0.0.1:%PORT%
echo  Close this window to stop the service.
echo.

%PY% "scripts\panel_server.py" --port %PORT%
if errorlevel 1 (
  echo.
  echo [hint] Failed to start. Common causes:
  echo   1. Port %PORT% is in use -- use another port: start-panel.bat 8766
  echo   2. Firewall is blocking -- the panel only listens on 127.0.0.1
  echo   3. Python is too old -- 3.9 or newer is required
  echo.
  pause
)

endlocal