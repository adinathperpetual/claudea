@echo off
title Contract Note Extractor - Test Finesse login
cd /d "%~dp0"
".venv\Scripts\python.exe" -m finesse_sync test-login
pause
