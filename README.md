# RGB Studio

Personal all-in-one RGB manager. One effects engine on one shared clock drives
every device in sync: OpenRGB devices (motherboard headers, RAM, GPU, NZXT
controllers, Corsair, ...), plus native drivers for an NZXT Kraken Elite V2
(pump ring + LCD) and a MonsGeek M1 V5 HE keyboard.

Also: mouse settings saved on the mouse (Logitech HID++: DPI, polling, buttons,
macros, G HUB-style actions) and keyboard settings saved on the keyboard
(MonsGeek HE: actuation, Rapid Trigger, remaps, macros) on the Peripherals page.

Windows only. Built for one person's PC - it works on others, but expect rough
edges with hardware it hasn't seen.

## Building from a fresh clone

Needs Node.js (LTS) and Python 3 - the Kraken, keyboard and mouse drivers are
small Python scripts: `py -m pip install hidapi pyusb libusb-package pillow`.

1. Clone, then double-click **Build RGB Studio.bat**. The first run installs the
   build tools (`npm install`) and downloads the OpenRGB backend into
   `bin\OpenRGB` - it isn't stored in this repo.
2. Optional: `tools\update-lhm.ps1` downloads LibreHardwareMonitor for CPU/GPU
   temperatures on the Kraken LCD overlay.
3. Optional: GIF search on the Kraken LCD needs a free GIPHY API key
   (developers.giphy.com) - paste it in Settings, or put
   `{ "giphyKey": "..." }` in `src\local-config.json` (git-ignored).

## Everyday use

| Double-click | What it does |
|---|---|
| **Build RGB Studio.bat** | After code changes: stops the app, rebuilds (~1 min), starts it. |
| **Build Installer.bat** | Makes `dist\RGB-Studio-Setup.exe` for another PC. |
| **Collect Diagnostics.bat** | Writes a report + logs to `diagnostics\` for debugging. |

The app is **`dist\win-unpacked\RGB Studio.exe`** - pin that to the taskbar /
Start. It handles admin rights itself:

- First run asks for admin once (UAC), then registers a "RGB Studio" scheduled
  task pointed at itself. Every later launch goes through that task: admin,
  no prompt.
- Starts with Windows (hidden in the tray). Toggle with the tray icon >
  **Start with Windows**.
- Opening it while it's already running just brings the window up.

## Other PCs

Run **Build Installer.bat**, copy `dist\RGB-Studio-Setup.exe` over, run it.
RAM / motherboard lighting needs the free PawnIO driver (https://pawnio.eu);
the app shows a banner with a download button if it's missing. Kraken and
MonsGeek support only switch on when that hardware is plugged in.

## Layout

```
src\boot.js            entry: admin hand-off + single instance (launcher.js)
src\main.js            app: OpenRGB connection, effects loop, tray, IPC
src\effects.js         effects engine (+ RGBW packing for 4-channel strips)
src\renderer\          UI
src\vendor\openrgb-sdk patched SDK (OpenRGB 1.x flag fix)
src\assets\            app icon
bin\OpenRGB\           OpenRGB backend (bundled, runs hidden)
bin\sensors.ps1 + LHM  CPU/GPU temps for the LCD overlay
kraken\                Python drivers: Kraken ring/LCD, MonsGeek keyboard
peripherals\           Python driver: Logitech mouse DPI/polling/buttons/macros
build\                 exe/installer icon + installer script
tools\                 build.js, diagnose.ps1, update-openrgb.ps1, update-lhm.ps1
_archive\              old scripts/backups, no longer used (safe to delete)
```

## Notes

- Only one program can own a device: close iCUE, Armoury Crate, MSI Center,
  SignalRGB, NZXT CAM.
- Updating OpenRGB: quit the app, run
  `powershell -ExecutionPolicy Bypass -File tools\update-openrgb.ps1`, then
  Build RGB Studio.bat.
- Kraken: both USB interfaces bound to WinUSB (Zadig). The sensor overlay uses
  bucketless Q565 streaming; repeated bucket uploads can wedge the cooler into
  its bootloader (1e71:3011) - only a PSU power-off recovers that.
- Settings page: theme/accent, Start with Windows, RAM refresh (default 30 fps),
  Kraken pump ring mode (Synced = streamed per LED on the shared clock; Firmware
  = the cooler's own animations, smooth but not in sync) and ring refresh rate.
- Kraken ring: streamed with the cooler's live-frame command (`26 14`, one
  512-byte report per frame, paced by its ~2 ms acknowledgement) - documented
  from an NZXT CAM capture by ushineko/sanshoku (docs/kraken-lighting.md). The
  older `22`/`2A` per-LED commands are acknowledged only after ~0.4 s, which
  capped streaming at ~2 updates/s. "Cooler's own" mode uses the firmware
  effects (`2A 04`) with your colors and speed instead. OpenRGB's Kraken
  detector is switched off so it can't fight over the ring.
- LED counts: OpenRGB reports them for every device that can; plain ARGB
  headers can't (one-way data line), so those are set once under a device's
  LED setup and remembered. Direct mode is re-applied after sleep/unlock and
  whenever a card drops out of it (checked every 2 minutes, no blinking).
- Peripherals (Logitech HID++ mice): DPI speeds, polling rate, button
  assignments and macros are written to the mouse's onboard profile (feature
  0x8100), so they work with nothing running. Close G HUB first. Every save
  checks the profile CRC, backs up the original sector
  (`%APPDATA%\rgb-studio\peripheral-backups`), verifies by reading back, and
  "Restore original" puts it back. Macros live in a separate sector that's
  blank, already this profile's, or tagged `RGBS` (ours) - never another
  profile's. Macro names are kept in RGB Studio's settings (the mouse only
  stores the key sequence).
- Button actions: Windows shortcuts, media keys, ClipVault (its hotkeys are
  read from `%APPDATA%\ClipVault\settings.json`), Discord/OBS (the button
  presses Ctrl+F13-F19; link it once in their keybind settings) are all plain
  key presses stored on the mouse. "Apps & sites" buttons press Ctrl+Alt+F13-F24
  (then Ctrl+Shift+Alt), which RGB Studio catches as a global hotkey and opens
  through explorer.exe so the app runs as you, not as admin.
- MonsGeek M1 V5 HE (Peripherals page, wired): actuation / release points,
  Rapid Trigger (per key), dead zones, polling rate, key remaps (same action
  list as the mouse: Windows shortcuts, ClipVault, Discord/OBS, apps, media,
  macros) - all stored on the keyboard's active profile. Handled inside
  `kraken\monsgeek_he.py` (it already owns the keyboard for lighting), using the
  wire format documented by echtzeit-solutions/monsgeek-akko-linux. The first
  read saves the original tables + keymap to
  `%APPDATA%\rgb-studio\peripheral-backups\monsgeek-<id>-p<profile>-original.json`
  for "Restore original"; RGB Studio's keyboard macros use slots 15 and down
  (the MonsGeek app fills from 0). The Fn key is never remapped.
- Logs: `%APPDATA%\rgb-studio\app.log`, `kraken\driver.log`, `kraken\monsgeek.log`,
  `peripherals\peripherals.log`.

## License

RGB Studio is free software under the **GNU GPL v3.0 or later** (see `LICENSE`).
Parts of `kraken\nzxt_kraken.py` are ported from liquidctl (GPL-3.0), which is
one reason for this choice.

## Credits

RGB Studio stands on other people's reverse-engineering work. Thank you to:

- **OpenRGB** (GPL-2.0, https://gitlab.com/CalcProgrammer1/OpenRGB) - the
  backend that talks to most devices. Not included here: the build downloads
  the official release, and its source is at the link.
- **openrgb-sdk** by Mola19 (MIT) - vendored in `src\vendor\openrgb-sdk` with
  its license, patched for OpenRGB 1.x.
- **liquidctl** (GPL-3.0, https://github.com/liquidctl/liquidctl) - Kraken
  bucket/LCD transfer and lighting protocol.
- **cypherxdev77/nzxt-cam-linux** (MIT) - Kraken Elite 2024 LCD protocol.
- **LeProta/NZXTKrakenLCDPlugin** (MIT) - bucketless Q565 streaming and the
  shared-HID setup.
- **ushineko/sanshoku** - the Kraken live ring frame command (`26 14`) and
  firmware effects, documented from an NZXT CAM capture.
- **echtzeit-solutions/monsgeek-akko-linux** (GPL-3.0) - the MonsGeek / Akko
  HE keyboard protocol (screen sync, actuation, Rapid Trigger, key matrix,
  macros) and its firmware safety limits.
- **libratbag**, **Solaar** and **lowtech** - Logitech HID++ 2.0 onboard
  profile, button and macro formats.
- **LibreHardwareMonitor** (MPL-2.0) - optional CPU/GPU temperatures; downloaded,
  not included.
- **PawnIO** (https://pawnio.eu) - the SMBus driver RAM/motherboard lighting needs.
- **GIPHY** - GIF search. **Electron** / **electron-builder** - the app shell.

Not affiliated with NZXT, Logitech, MonsGeek, Razer or any other hardware
maker; product names are theirs.
