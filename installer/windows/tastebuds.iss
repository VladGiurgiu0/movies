; Tastebuds - Windows installer (Inno Setup 6.3 or newer).
;
; Built by .github/workflows/release.yml after PyInstaller has produced
; dist\Tastebuds\. By hand, from the repo root:
;     iscc /DAppVersion=1.0.0 installer\windows\tastebuds.iss
;
; Installs for the current user only (no administrator rights needed) and adds
; a Start-menu entry, an optional desktop icon, and an uninstaller. Your data
; lives in %USERPROFILE%\Tastebuds and is never touched by install/uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8B0F3C2E-5A7D-4E8B-9C1F-7A2D6E4B9C31}
AppName=Tastebuds
AppVersion={#AppVersion}
AppVerName=Tastebuds {#AppVersion}
AppPublisher=Vlad Giurgiu
AppPublisherURL=https://github.com/VladGiurgiu0/movies
AppSupportURL=https://github.com/VladGiurgiu0/movies
DefaultDirName={localappdata}\Programs\Tastebuds
DefaultGroupName=Tastebuds
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=Tastebuds-Windows-Setup
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\Tastebuds.exe
UninstallDisplayName=Tastebuds
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\Tastebuds\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; a clean slate for the program files on update (never the data folder)
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\Tastebuds"; Filename: "{app}\Tastebuds.exe"
Name: "{autodesktop}\Tastebuds"; Filename: "{app}\Tastebuds.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Tastebuds.exe"; Description: "{cm:LaunchProgram,Tastebuds}"; Flags: nowait postinstall skipifsilent
