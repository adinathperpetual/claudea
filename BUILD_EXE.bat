@echo off
title Contract Note Extractor - build the .exe
cd /d "%~dp0"
echo ============================================================
echo   Contract Note Extractor - build ContractNoteExtractor.exe
echo ============================================================
if exist ".venv\Scripts\python.exe" goto :haveenv
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || goto :nopython
echo [1/3] Creating the build environment...
%PY% -m venv .venv || goto :fail

:haveenv
echo [2/3] Installing components (a few minutes the first time)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller || goto :fail
echo [3/3] Building the .exe (about 2 minutes)...
".venv\Scripts\python.exe" packaging\exe\build_exe.py || goto :fail
echo.
echo Done. The program is in:  dist\exe\ContractNoteExtractor\ContractNoteExtractor.exe
echo The zip to give to other PCs:  dist\ContractNoteExtractor-exe.zip
start "" "dist"
pause
exit /b 0

:fail
echo.
echo  The build did not finish. Check the internet connection and run BUILD_EXE.bat again.
echo  If it keeps failing, send a photo of this window to IT.
pause
exit /b 1

:nopython
echo.
echo  Building the .exe needs Python 3.10 or newer on THIS PC only
echo  (the PCs that run the .exe do not need Python).
echo  1. Install it from https://www.python.org/downloads/
echo     (tick "Add python.exe to PATH" on the first screen)
echo  2. Then double-click BUILD_EXE.bat again.
start "" https://www.python.org/downloads/
pause
exit /b 1
