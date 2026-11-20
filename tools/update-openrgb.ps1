# Downloads the OpenRGB backend (stable 1.1) into bin\OpenRGB.
# "Build RGB Studio.bat" runs this automatically when bin\OpenRGB is missing.
# To upgrade later, quit RGB Studio and run:
#   powershell -ExecutionPolicy Bypass -File tools\update-openrgb.ps1
# then "Build RGB Studio.bat". The old version is kept as bin\OpenRGB.previous.
#
# OpenRGB 1.x needs the PawnIO driver for RAM / motherboard (SMBus) devices:
# https://pawnio.eu  (install once per PC; RGB Studio must run as admin).

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# OpenRGB moved its releases from GitLab to Codeberg.
$url = 'https://codeberg.org/OpenRGB/OpenRGB/releases/download/release_1.1/OpenRGB_1.1_Windows_64_8e16de2.zip'

if (Get-Process 'RGB Studio', 'OpenRGB' -ErrorAction SilentlyContinue) {
    throw 'Quit RGB Studio first (tray icon > Quit). If it keeps running, use an admin cmd: taskkill /f /t /im "RGB Studio.exe"'
}

$zip = Join-Path $env:TEMP 'openrgb_1.1.zip'
Write-Host "Downloading OpenRGB 1.1 ..."
Invoke-WebRequest $url -OutFile $zip -UseBasicParsing

$tmp = Join-Path $env:TEMP 'openrgb_1.1_extract'
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive $zip -DestinationPath $tmp -Force
$exe = Get-ChildItem $tmp -Recurse -Filter 'OpenRGB.exe' | Select-Object -First 1
if (-not $exe) { throw 'OpenRGB.exe not found inside the downloaded zip.' }
$srcDir = $exe.DirectoryName

$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root 'bin\OpenRGB'
$backup = Join-Path $root 'bin\OpenRGB.previous'

# Already on exactly this version? Then change nothing (and never touch the backup).
$curExe = Join-Path $dest 'OpenRGB.exe'
if ((Test-Path $curExe) -and
    ((Get-FileHash $curExe).Hash -eq (Get-FileHash $exe.FullName).Hash)) {
    Remove-Item $tmp -Recurse -Force
    Remove-Item $zip
    Write-Host 'You already have this OpenRGB version - nothing changed.' -ForegroundColor Green
    exit 0
}

if (Test-Path $dest) {
    if (Test-Path $backup) {
        # Never overwrite an existing backup (it may be the only copy of an
        # older version). Park the current one under a dated name instead.
        $park = Join-Path $root ('bin\OpenRGB.replaced-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        Move-Item $dest $park
        Write-Host "Current OpenRGB kept at: $park  (existing backup left untouched)"
    } else {
        Move-Item $dest $backup
        Write-Host "Previous OpenRGB kept at: $backup"
    }
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item (Join-Path $srcDir '*') $dest -Recurse -Force
Remove-Item $tmp -Recurse -Force
Remove-Item $zip

Write-Host ''
Write-Host "OpenRGB 1.1 installed to: $dest" -ForegroundColor Green
Write-Host 'Next: double-click "Build RGB Studio.bat" so the app bundles it.'
Write-Host 'If RAM / motherboard lighting disappears afterwards, install PawnIO from https://pawnio.eu'
