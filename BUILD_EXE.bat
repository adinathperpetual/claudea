@echo off
title Contract Note Extractor - build the installer
cd /d "%~dp0"
echo ============================================================
echo   Contract Note Extractor - build ContractNoteExtractor-Setup.exe
echo ============================================================
if exist ".venv\Scripts\python.exe" goto :haveenv
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || goto :nopython
echo [1/4] Creating the build environment...
%PY% -m venv .venv || goto :fail

:haveenv
echo [2/4] Installing components (a few minutes the first time)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller || goto :fail
echo [3/4] Checking Inno Setup (makes the installer)...
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" goto :haveinno
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" goto :haveinno
where ISCC >nul 2>nul && goto :haveinno
where winget >nul 2>nul || goto :noinno
winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" goto :haveinno
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" goto :haveinno
goto :noinno

:haveinno
echo [4/4] Building the installer (about 3 minutes)...
".venv\Scripts\python.exe" packaging\exe\build_exe.py || goto :fail
if not exist "dist\ContractNoteExtractor-Setup.exe" goto :fail
echo.
echo Done:  dist\ContractNoteExtractor-Setup.exe
echo Give that one file to the other PCs; it installs everything.
start "" "dist"
pause
exit /b 0

:fail
echo.
echo  The build did not finish. Check the internet connection and run BUILD_EXE.bat again.
echo  If it keeps failing, send a photo of this window to IT.
pause
exit /b 1

:noinno
echo.
echo  Inno Setup 6 is needed to make the installer.
echo  1. Install it from https://jrsoftware.org/isdl.php (free, just click Next)
echo  2. Then double-click BUILD_EXE.bat again.
start "" https://jrsoftware.org/isdl.php
pause
exit /b 1

:nopython
echo.
echo  Building the installer needs Python 3.10 or newer on THIS PC only
echo  (the PCs that install the tool do not need Python).
echo  1. Install it from https://www.python.org/downloads/
echo     (tick "Add python.exe to PATH" on the first screen)
echo  2. Then double-click BUILD_EXE.bat again.
start "" https://www.python.org/downloads/
pause
exit /b 1
