; Windows installer for Contract Note Extractor (Inno Setup 6).
; Built by packaging/exe/build_exe.py after the .exe, or:  ISCC packaging\installer\ContractNoteExtractor.iss
; Installs per user (no admin rights), so the tool can write .env and data\ next to the .exe.
; Upgrades install over the old version and keep .env and data\; uninstall keeps them too.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Contract Note Extractor"
#define AppExe "ContractNoteExtractor.exe"
#define SrcDir "..\..\dist\exe\ContractNoteExtractor"

[Setup]
AppId={{6B0E3C1A-6C55-4B8B-9E0B-3C2F6D1E7A41}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Perpetual Investments
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=ContractNoteExtractor-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExe}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start the tool when Windows starts (keeps the 08:00 Finesse sync running)"; GroupDescription: "Start-up:"; Flags: unchecked

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Change Finesse login (setup)"; Filename: "{app}\{#AppExe}"; Parameters: "setup"; WorkingDir: "{app}"
Name: "{group}\Test Finesse login"; Filename: "{app}\TEST_LOGIN.bat"; WorkingDir: "{app}"
Name: "{group}\Sync clients from Finesse now"; Filename: "{app}\SYNC_NOW.bat"; WorkingDir: "{app}"
Name: "{group}\Access tokens and key backup"; Filename: "{app}\data\ACCESS_TOKENS.txt"
Name: "{group}\Read me first"; Filename: "{app}\START_HERE.txt"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "serve"; WorkingDir: "{app}"; Flags: runminimized; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Description: "Open {#AppName} now (the first time it asks for the Finesse login)"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
