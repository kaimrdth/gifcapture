; Inno Setup script for GIF Capture. Built by ..\build.ps1, which passes AppDir, FFmpeg,
; FFmpegLicense, IconFile, OutDir and AppVersion with /D.

#define AppName "GIF Capture"

[Setup]
AppId={{8F3C2A51-6B7E-4D3A-9C1F-2E5B7A9D4C10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\Programs\GifCapture
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=GifCapture-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\GifCapture.exe
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Upgrading over a running copy: close it, install, then the [Run] entry starts the new one.
CloseApplications=force
RestartApplications=no

[Messages]
WelcomeLabel2=This will install [name] on your computer.%n%nPress Win+Shift+D, drag over part of your screen, and it records up to 30 seconds, lets you trim, and copies a high-quality GIF to your clipboard.%n%nNo admin rights needed.

[Tasks]
Name: startup; Description: "Start GIF Capture automatically when I sign in"

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#FFmpeg}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#FFmpegLicense}"; DestDir: "{app}"; DestName: "FFMPEG-LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\GifCapture.exe"
Name: "{userstartup}\{#AppName}"; Filename: "{app}\GifCapture.exe"; Tasks: startup

[Run]
Filename: "{app}\GifCapture.exe"; Description: "Start GIF Capture now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/im GifCapture.exe /f"; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\GifCapture"
