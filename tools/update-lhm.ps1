# Downloads LibreHardwareMonitor into bin\LHM so the LCD overlay can read
# CPU (and non-NVIDIA GPU) temperatures. Run once:
#   powershell -ExecutionPolicy Bypass -File tools\update-lhm.ps1
# Then restart RGB Studio.

$ErrorActionPreference = 'Stop'
$dest = Join-Path (Split-Path -Parent $PSScriptRoot) 'bin\LHM'
New-Item -ItemType Directory -Force -Path $dest | Out-Null

$zip = Join-Path $env:TEMP 'lhm.zip'
Write-Host 'Downloading LibreHardwareMonitor v0.9.6...'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip' -OutFile $zip
Expand-Archive $zip -DestinationPath $dest -Force
Remove-Item $zip

Write-Host "Done -> $dest"
Write-Host 'Restart RGB Studio; CPU/GPU temps will appear in the LCD overlay editor.'
