# Builds dist\GifCapture-Setup.exe: the one file colleagues need.
#   powershell -ExecutionPolicy Bypass -File build.ps1 [-Version 1.0.1]
# Needs: Python 3, ffmpeg on PATH, Inno Setup 6 (winget install JRSoftware.InnoSetup --scope user).
param([string]$Version = "1.0.0")
$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$work = Join-Path $env:LOCALAPPDATA "GifCapture-build"   # kept out of OneDrive
$venv = Join-Path $work "venv"

# Real interpreter path (the WindowsApps alias can't see every folder).
$py = & python -c "import sys; print(sys.executable)"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    & $py -m venv $venv
    & "$venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check pyinstaller
}

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup not found. Install it: winget install JRSoftware.InnoSetup --scope user" }

$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$ffLicense = Join-Path (Split-Path (Split-Path $ffmpeg)) "LICENSE"
if (-not (Test-Path $ffLicense)) { $ffLicense = Join-Path $work "FFMPEG-LICENSE.txt"; "FFmpeg is licensed under the GPL. See https://ffmpeg.org/legal.html" | Set-Content $ffLicense }

# Icon (same one the app draws for its tray icon).
$icon = Join-Path $work "icon.ico"
& "$venv\Scripts\python.exe" -c "import runpy, sys, pathlib; runpy.run_path(sys.argv[1])['write_icon'](pathlib.Path(sys.argv[2]))" (Join-Path $root "gifcapture.pyw") $icon

# 1) Python app -> GifCapture.exe (one folder, fast start-up).
& "$venv\Scripts\pyinstaller.exe" --noconfirm --clean --log-level WARN --windowed --name GifCapture `
    --icon $icon --distpath "$work\dist" --workpath "$work\build" --specpath $work (Join-Path $root "gifcapture.pyw")
if ($LASTEXITCODE) { throw "PyInstaller failed" }

# 1b) Command line for agents and scripts -> gifcap.exe (console). It imports gifcapture.pyw.
& "$venv\Scripts\pyinstaller.exe" --noconfirm --clean --log-level WARN --console --name gifcap `
    --paths $root --hidden-import gifcapture --icon $icon --distpath "$work\dist" --workpath "$work\build-cli" `
    --specpath $work (Join-Path $root "gifcap.py")
if ($LASTEXITCODE) { throw "PyInstaller (gifcap) failed" }

# 2) App + CLI + ffmpeg -> GifCapture-Setup.exe
$out = Join-Path $root "dist"
& $iscc /Q "/DAppVersion=$Version" "/DAppDir=$work\dist\GifCapture" "/DCliDir=$work\dist\gifcap" `
    "/DFFmpeg=$ffmpeg" "/DFFmpegLicense=$ffLicense" `
    "/DIconFile=$icon" "/DOutDir=$out" (Join-Path $root "packaging\installer.iss")
if ($LASTEXITCODE) { throw "Inno Setup failed" }

$setup = Get-Item (Join-Path $out "GifCapture-Setup.exe")
"Built {0} ({1:N0} MB)" -f $setup.FullName, ($setup.Length / 1MB)
