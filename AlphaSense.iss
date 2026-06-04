; ─────────────────────────────────────────────────────────────────
;  AlphaSense  —  Inno Setup Script  v11
;  Installs:
;    AlphaSense.exe  +  arduino-cli  +  all firmware sketches
;    +  CP210x driver (silent)  +  CH340 driver (silent)
;    +  Start Menu / Desktop shortcuts
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
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; ── Main executable ───────────────────────────────────────────────
Source: "AlphaSense.exe";                                     DestDir: "{app}";                                    Flags: ignoreversion

; ── Arduino CLI (required for OTA firmware updates) ───────────────
Source: "arduino-cli_1.5.0_Windows_64bit\arduino-cli.exe";   DestDir: "{app}\arduino-cli_1.5.0_Windows_64bit";    Flags: ignoreversion

; ── Firmware sketches ─────────────────────────────────────────────
Source: "AlphaSense_v2\AlphaSense_v2.ino";                   DestDir: "{app}\AlphaSense_v2";                      Flags: ignoreversion
Source: "AlphaSense_v1\AlphaSense_v1.ino";                   DestDir: "{app}\AlphaSense_v1";                      Flags: ignoreversion
Source: "AlphaSense_bluetooth\AlphaSense_bluetooth.ino";      DestDir: "{app}\AlphaSense_bluetooth";               Flags: ignoreversion
Source: "AlphaSense_usb\AlphaSense_usb.ino";                  DestDir: "{app}\AlphaSense_usb";                     Flags: ignoreversion

; ── Documentation ─────────────────────────────────────────────────
Source: "README.txt";                                         DestDir: "{app}";                                    Flags: ignoreversion

; ── USB drivers (extracted to temp, installed silently, then deleted) ──
Source: "drivers\CP210x\CP210xVCPInstaller_x64.exe";         DestDir: "{tmp}";                                    Flags: ignoreversion deleteafterinstall
Source: "drivers\CP210x\CP210xVCPInstaller_x86.exe";         DestDir: "{tmp}";                                    Flags: ignoreversion deleteafterinstall
Source: "drivers\CH340\CH341SER.EXE";                        DestDir: "{tmp}";                                    Flags: ignoreversion deleteafterinstall

[Icons]
; Start Menu
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Comment: "Launch AlphaSense Dashboard"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
; Desktop (optional — user must tick the checkbox)
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon; Comment: "Launch AlphaSense Dashboard"

[Run]
; ── Install CP210x USB driver silently ────────────────────────────
Filename: "{tmp}\CP210xVCPInstaller_x64.exe"; Parameters: "/S"; StatusMsg: "Installing Silicon Labs CP210x USB Driver..."; Flags: waituntilterminated runhidden; Check: IsWin64
Filename: "{tmp}\CP210xVCPInstaller_x86.exe"; Parameters: "/S"; StatusMsg: "Installing Silicon Labs CP210x USB Driver..."; Flags: waituntilterminated runhidden; Check: not IsWin64

; ── Install CH340/CH341 USB driver silently ───────────────────────
Filename: "{tmp}\CH341SER.EXE"; Parameters: "/S"; StatusMsg: "Installing WCH CH340/CH341 USB Driver..."; Flags: waituntilterminated runhidden

; ── Offer to launch after install ─────────────────────────────────
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#AppExeName}"; Parameters: "/uninstall"; Flags: skipifdoesntexist runhidden

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
