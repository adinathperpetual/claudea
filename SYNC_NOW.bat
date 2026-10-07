@echo off
title Contract Note Extractor - Sync from Finesse
cd /d "%~dp0"
".venv\Scripts\python.exe" -m finesse_sync sync
pause
