; ─────────────────────────────────────────────────────────────────
;  AlphaSense  —  Inno Setup Script
;  Installs: AlphaSense.exe + USB drivers + arduino-cli + sketches
; ─────────────────────────────────────────────────────────────────

#define AppName      "AlphaSense"
#define AppVersion   "1.0.0"
#define AppPublisher "Alphaion"
#define AppExeName   "AlphaSense.exe"
#define AppURL       "https://alphaion.com"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
LicenseFile=
OutputDir=Output
OutputBaseFilename=AlphaSense_Setup
SetupIconFile=icon_fixed.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
WizardImageFile=wizard_large.bmp
WizardSmallImageFile=wizard_small.bmp
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Installer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";    Description: "{cm:CreateDesktopIcon}";    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked; OnlyBelowVersion: 6.1

[Files]
; Main executable
Source: "AlphaSense.exe";                                          DestDir: "{app}";                         Flags: ignoreversion

; Arduino CLI
Source: "arduino-cli_1.5.0_Windows_64bit\arduino-cli.exe";        DestDir: "{app}\arduino-cli_1.5.0_Windows_64bit"; Flags: ignoreversion

; Firmware sketches
Source: "AlphaSense_v2\AlphaSense_v2.ino";                        DestDir: "{app}\AlphaSense_v2";           Flags: ignoreversion
Source: "AlphaSense_v1\AlphaSense_v1.ino";                        DestDir: "{app}\AlphaSense_v1";           Flags: ignoreversion

; Drivers (extracted silently during install via [Run])
Source: "drivers\CP210x\CP210xVCPInstaller_x64.exe";              DestDir: "{tmp}";                         Flags: ignoreversion deleteafterinstall
Source: "drivers\CP210x\CP210xVCPInstaller_x86.exe";              DestDir: "{tmp}";                         Flags: ignoreversion deleteafterinstall
Source: "drivers\CH340\CH341SER.EXE";                             DestDir: "{tmp}";                         Flags: ignoreversion deleteafterinstall

[Icons]
Name: "{group}\{#AppName}";              Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}";    Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";        Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Install CP210x driver silently
Filename: "{tmp}\CP210xVCPInstaller_x64.exe"; Parameters: "/S"; StatusMsg: "Installing Silicon Labs CP210x USB Driver..."; Flags: waituntilterminated runhidden; Check: IsWin64
Filename: "{tmp}\CP210xVCPInstaller_x86.exe"; Parameters: "/S"; StatusMsg: "Installing Silicon Labs CP210x USB Driver..."; Flags: waituntilterminated runhidden; Check: not IsWin64

; Install CH340 driver silently
Filename: "{tmp}\CH341SER.EXE"; Parameters: "/S"; StatusMsg: "Installing WCH CH340/CH341 USB Driver..."; Flags: waituntilterminated runhidden

; Launch app after install
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#AppExeName}"; Parameters: "/uninstall"; Flags: skipifdoesntexist

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
