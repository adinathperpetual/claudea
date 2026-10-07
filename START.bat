@echo off
title Contract Note Extractor - keep this window open
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setup has not been run yet. Double-click SETUP.bat first.
  pause
  exit /b 1
)
if not exist ".env" (
  echo Settings are missing. Double-click SETUP.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m finesse_sync serve --open
echo.
echo The tool has stopped. Close this window, or press a key.
pause
