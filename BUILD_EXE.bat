@echo off
title Contract Note Extractor - build the .exe
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run SETUP.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q pyinstaller || goto :fail
".venv\Scripts\python.exe" packaging\exe\build_exe.py || goto :fail
echo.
echo Done: dist\ContractNoteExtractor-exe.zip
pause
exit /b 0
:fail
echo Build failed.
pause
exit /b 1
