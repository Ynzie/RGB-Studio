// Detects which special (non-OpenRGB) hardware is actually plugged in, so the
// app works on any PC: no Kraken -> no pump-ring card, no LCD tab, no Kraken
// driver/Python probing; no MonsGeek HE -> its driver isn't started.
//
// Uses Windows' own device list (Win32_PnPEntity), which works regardless of
// which driver (HID, WinUSB via Zadig, ...) the device is bound to.
//
// Also reports whether the PawnIO driver is installed: OpenRGB 1.x needs it
// for RAM and motherboard (SMBus) lighting, so a PC without it shows only
// USB/GPU devices.

const { execFile } = require('child_process');

const NZXT_VID = '1E71';
const KRAKEN_PIDS = ['3008', '3009', '300A', '300C', '300E', '3012', '3013'];
const KRAKEN_BOOTLOADER_PID = '3011';
const MONSGEEK_HE = ['3151:5030']; // M1 V5 HE, wired

// Returns { ids: Set of "VID:PID" (uppercase hex), pawnio: bool }, or null.
function listUsbIds() {
  return new Promise((resolve) => {
    if (process.platform !== 'win32') return resolve(null);
    execFile(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-Command',
        "Get-CimInstance -ClassName Win32_PnPEntity | ForEach-Object { $_.DeviceID }; " +
        "'PAWNIO=' + [int]((Test-Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\PawnIO') -or " +
        "(Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\PawnIO'))"],
      { windowsHide: true, timeout: 30000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout) => {
        if (err) return resolve(null);
        const ids = new Set();
        for (const m of String(stdout).matchAll(/VID_([0-9A-F]{4})&PID_([0-9A-F]{4})/gi)) {
          ids.add((m[1] + ':' + m[2]).toUpperCase());
        }
        const pw = /PAWNIO=([01])/.exec(String(stdout));
        resolve({ ids, pawnio: pw ? pw[1] === '1' : null });
      }
    );
  });
}

// { kraken, krakenBootloader, monsgeek, pawnio } — each true/false, or null =
// unknown (detection failed: callers then behave as before and just try).
async function detect() {
  const r = await listUsbIds();
  if (!r) return { kraken: null, krakenBootloader: false, monsgeek: null, pawnio: null };
  const { ids, pawnio } = r;
  return {
    pawnio,
    kraken: KRAKEN_PIDS.some((p) => ids.has(NZXT_VID + ':' + p)),
    krakenBootloader: ids.has(NZXT_VID + ':' + KRAKEN_BOOTLOADER_PID),
    monsgeek: MONSGEEK_HE.some((id) => ids.has(id)),
  };
}

module.exports = { detect };
