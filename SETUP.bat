@echo off
title Contract Note Extractor - Setup
cd /d "%~dp0"
echo ============================================================
echo   Contract Note Extractor - first-time setup
echo ============================================================
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || goto :nopython

echo [1/4] Creating the program environment...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv || goto :fail
echo [2/4] Installing components (a few minutes the first time)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail
echo [3/4] Installing the browser used to read Finesse...
".venv\Scripts\python.exe" -m playwright install chromium || goto :fail
echo [4/4] Finesse login and security keys...
".venv\Scripts\python.exe" -m finesse_sync setup || goto :fail
echo.
echo Setup finished. Double-click START.bat to open the tool.
pause
exit /b 0

:fail
echo.
echo  Setup did not finish. Check the internet connection and run SETUP.bat again.
echo  If it keeps failing, send a photo of this window to IT.
pause
exit /b 1

:nopython
echo.
echo  Python 3.10 or newer is not installed.
echo  1. Install it from https://www.python.org/downloads/
echo     (tick "Add python.exe to PATH" on the first screen)
echo  2. Then double-click SETUP.bat again.
start "" https://www.python.org/downloads/
pause
exit /b 1
