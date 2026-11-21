@echo off
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 (
  py -3.11 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=py -3.11"
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Python was not found. Install Python 3.11 from https://www.python.org/downloads/windows/ and run setup.bat again.
  goto :fail
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating .venv ...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
) else (
  echo [1/3] .venv already exists
)

echo [2/3] Installing Python packages ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :fail

echo [3/3] Runtime, model and MCP config ...
".venv\Scripts\python.exe" scripts\install.py %*
if errorlevel 1 goto :fail

if not defined CUA_SETUP_NO_PAUSE pause
exit /b 0

:fail
echo.
echo Setup did not finish. Fix the problem above and run setup.bat again; finished steps are skipped.
if not defined CUA_SETUP_NO_PAUSE pause
exit /b 1
