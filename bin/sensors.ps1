# Sensor helper for RGB Studio's LCD overlay: prints one JSON line {cpu,gpu} per tick.
#
# CPU temps need LibreHardwareMonitorLib.dll in .\LHM - run tools\update-lhm.ps1 once to
# fetch it. Without it, GPU temp still works on NVIDIA cards via nvidia-smi.
# The app runs this elevated (required for CPU sensor access).
param([switch]$Loop, [int]$IntervalMs = 2000)

$ErrorActionPreference = 'SilentlyContinue'

$lhm = $null
$dll = Join-Path $PSScriptRoot 'LHM\LibreHardwareMonitorLib.dll'
if (Test-Path $dll) {
    try {
        Add-Type -Path $dll
        $lhm = New-Object LibreHardwareMonitor.Hardware.Computer
        $lhm.IsCpuEnabled = $true
        $lhm.IsGpuEnabled = $true
        $lhm.Open()
    } catch { $lhm = $null }
}

function Get-NvidiaGpuTemp {
    try {
        $out = & nvidia-smi --query-gpu=temperature.gpu --format=csv, noheader, nounits 2>$null
        if ($out) { return [double](($out | Select-Object -First 1).ToString().Trim()) }
    } catch {}
    return $null
}

function Read-Temps {
    $cpu = $null; $gpu = $null
    if ($lhm) {
        foreach ($hw in $lhm.Hardware) {
            $hw.Update()
            $temps = @($hw.Sensors | Where-Object { $_.SensorType -eq 'Temperature' -and $_.Value })
            if ($temps.Count -eq 0) { continue }
            if ("$($hw.HardwareType)" -eq 'Cpu') {
                $t = $temps | Where-Object { $_.Name -match 'Package|Tctl|Average' } | Select-Object -First 1
                if (-not $t) { $t = $temps | Select-Object -First 1 }
                $cpu = [math]::Round([double]$t.Value, 1)
            } elseif ("$($hw.HardwareType)" -match 'Gpu') {
                $t = $temps | Where-Object { $_.Name -match 'Core' } | Select-Object -First 1
                if (-not $t) { $t = $temps | Select-Object -First 1 }
                $gpu = [math]::Round([double]$t.Value, 1)
            }
        }
    }
    if ($null -eq $gpu) { $gpu = Get-NvidiaGpuTemp }
    @{ cpu = $cpu; gpu = $gpu } | ConvertTo-Json -Compress
}

if ($Loop) {
    while ($true) { Read-Temps; Start-Sleep -Milliseconds $IntervalMs }
} else {
    Read-Temps
}
