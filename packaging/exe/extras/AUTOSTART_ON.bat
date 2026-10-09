@echo off
cd /d "%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
(
  echo @echo off
  echo cd /d "%~dp0"
  echo start "Contract Note Extractor" /min "%~dp0ContractNoteExtractor.exe" serve
) > "%STARTUP%\ContractNoteExtractor.bat"
echo The tool will now start automatically when this PC starts (daily sync keeps running).
echo To undo, run AUTOSTART_OFF.bat.
pause
