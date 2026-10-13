; Windows installer for Contract Note Extractor (Inno Setup 6).
; Built by packaging/exe/build_exe.py after the .exe, or:  ISCC packaging\installer\ContractNoteExtractor.iss
; Program files go to C:\Program Files\Contract Note Extractor. Settings (.env) and data\ go to
; C:\ProgramData\Contract Note Extractor, writable for every user of the PC; upgrades and
; uninstall leave them in place.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Contract Note Extractor"
#define AppExe "ContractNoteExtractor.exe"
#define SrcDir "..\..\dist\exe\ContractNoteExtractor"
#define DataDir "{commonappdata}\Contract Note Extractor"

[Setup]
AppId={{6B0E3C1A-6C55-4B8B-9E0B-3C2F6D1E7A41}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Perpetual Investments
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=admin
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

[Dirs]
Name: "{#DataDir}"; Permissions: users-modify; Flags: uninsneveruninstall

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{#DataDir}"
Name: "{group}\Change Finesse login"; Filename: "{app}\{#AppExe}"; Parameters: "setup --pause"; WorkingDir: "{#DataDir}"
Name: "{group}\Test Finesse login"; Filename: "{app}\{#AppExe}"; Parameters: "test-login --pause"; WorkingDir: "{#DataDir}"
Name: "{group}\Sync clients from Finesse now"; Filename: "{app}\{#AppExe}"; Parameters: "sync --pause"; WorkingDir: "{#DataDir}"
Name: "{group}\Settings and data folder"; Filename: "{#DataDir}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{#DataDir}"; Tasks: desktopicon
Name: "{commonstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "serve"; WorkingDir: "{#DataDir}"; Flags: runminimized; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; WorkingDir: "{#DataDir}"; Description: "Open {#AppName} now (the first time it asks for the Finesse login)"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
