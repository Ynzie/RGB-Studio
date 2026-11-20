# Collects everything needed to debug RGB Studio / OpenRGB into .\diagnostics
# (report.txt + copies of the logs). Read-only: changes nothing on the system.
# Usually run via "Collect Diagnostics.bat".

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$out = Join-Path $root 'diagnostics'
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force -Path $out | Out-Null
$report = Join-Path $out 'report.txt'
function W($s) { Add-Content -Path $report -Value $s -Encoding ASCII }
function Section($s) { W ''; W ('==== ' + $s + ' ====') }

W ('RGB Studio diagnostics  ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
W ('Windows: ' + [Environment]::OSVersion.VersionString)

Section 'Processes'
Get-Process 'RGB Studio', 'OpenRGB', 'python*', 'py' -ErrorAction SilentlyContinue |
    ForEach-Object { W ('{0}  pid={1}  {2}' -f $_.ProcessName, $_.Id, $_.Path) }

Section 'OpenRGB SDK port 6742'
$l = Get-NetTCPConnection -LocalPort 6742 -State Listen -ErrorAction SilentlyContinue
if ($l) { W ('LISTENING (pid ' + ($l | Select-Object -First 1).OwningProcess + ')') } else { W 'NOT listening' }

Section 'Bundled OpenRGB versions'
foreach ($p in @('bin\OpenRGB\OpenRGB.exe', 'bin\OpenRGB.previous\OpenRGB.exe', 'bin\OpenRGB.1.1\OpenRGB.exe',
                 'dist\win-unpacked\resources\bin\OpenRGB\OpenRGB.exe')) {
    $f = Join-Path $root $p
    if (Test-Path $f) {
        $v = (Get-Item $f).VersionInfo
        W ('{0}: {1} / {2}  ({3})' -f $p, $v.FileVersion, $v.ProductVersion, (Get-Item $f).LastWriteTime)
    }
}

Section 'Scheduled task "RGB Studio" (prompt-free admin start)'
$t = Get-ScheduledTask -TaskName 'RGB Studio' -ErrorAction SilentlyContinue
if ($t) {
    W ('exe: ' + $t.Actions[0].Execute + ' ' + $t.Actions[0].Arguments)
    W ('logon trigger: ' + [bool](($t.Triggers | Measure-Object).Count) + '  instances: ' + $t.Settings.MultipleInstances + '  priority: ' + $t.Settings.Priority + '  state: ' + $t.State)
} else { W 'not registered' }

Section 'PawnIO driver'
$svc = Get-CimInstance Win32_SystemDriver -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'PawnIO*' }
if ($svc) {
    foreach ($s in $svc) {
        W ('{0}: state={1} start={2} path={3}' -f $s.Name, $s.State, $s.StartMode, $s.PathName)
        $drvPath = ($s.PathName -replace '^\\\?\?\\', '' -replace '^\\SystemRoot', $env:SystemRoot)
        if (Test-Path $drvPath) { W ('  version: ' + (Get-Item $drvPath).VersionInfo.FileVersion) }
    }
} else { W 'PawnIO NOT installed' }

Section 'Visual C++ runtime (Qt6 OpenRGB needs a recent one)'
foreach ($d in @('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')) {
    $f = Join-Path $env:SystemRoot ('System32\' + $d)
    if (Test-Path $f) { W ($d + ': ' + (Get-Item $f).VersionInfo.FileVersion) } else { W ($d + ': MISSING') }
}

Section 'Recent application errors mentioning OpenRGB / RGB Studio (last 6 h)'
try {
    Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Level = 2; StartTime = (Get-Date).AddHours(-6) } -ErrorAction Stop |
        Where-Object { $_.Message -match 'OpenRGB|RGB Studio' } | Select-Object -First 10 |
        ForEach-Object { W ('[' + $_.TimeCreated + '] ' + ($_.Message -replace '\s+', ' ')) }
} catch { W '(none)' }

Section 'USB devices (VID:PID)'
$ids = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | ForEach-Object { $_.DeviceID } |
    Select-String -Pattern 'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})' -AllMatches |
    ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value + ':' + $_.Groups[2].Value } | Sort-Object -Unique
W ($ids -join '  ')

# ---- copy logs / configs ----
$ud = Join-Path $env:APPDATA 'rgb-studio'
foreach ($f in @('app.log', 'app.log.old', 'settings.json', 'state.json', 'zones.json', 'zonefmt.json')) {
    $src = Join-Path $ud $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $out $f) }
}
$cfg = Join-Path $ud 'openrgb-config'
if (Test-Path $cfg) {
    if (Test-Path (Join-Path $cfg 'OpenRGB.json')) { Copy-Item (Join-Path $cfg 'OpenRGB.json') (Join-Path $out 'OpenRGB.json') }
    $logs = Get-ChildItem $cfg -Recurse -Filter '*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 3
    $i = 0
    foreach ($lg in $logs) { $i++; Copy-Item $lg.FullName (Join-Path $out ('openrgb_' + $i + '_' + $lg.Name)) }
    Section 'OpenRGB config dir'
    Get-ChildItem $cfg -Recurse -ErrorAction SilentlyContinue | Select-Object -First 40 |
        ForEach-Object { W ($_.FullName.Substring($cfg.Length) + '  ' + $_.LastWriteTime) }
}
foreach ($f in @('kraken\driver.log', 'kraken\monsgeek.log')) {
    $src = Join-Path $root $f
    if (Test-Path $src) {
        Get-Content $src -Tail 200 | Set-Content (Join-Path $out ((Split-Path $f -Leaf) + '.tail.txt')) -Encoding ASCII
    }
}

Write-Host "Diagnostics saved to: $out" -ForegroundColor Green
