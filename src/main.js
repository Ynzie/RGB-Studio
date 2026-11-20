// Loaded by boot.js once this process is the one (elevated) copy of the app.
const { app, BrowserWindow, ipcMain, dialog, Tray, Menu, nativeImage, powerMonitor, shell, globalShortcut } = require('electron');
const path = require('path');
const fs = require('fs');
const net = require('net');
const { spawn } = require('child_process');
const launcher = require('./launcher');
app.setAppUserModelId('com.anthony.rgbstudio');
// Vendored + patched openrgb-sdk (see src/vendor/openrgb-sdk/device.js): the
// npm 0.6.0 build crashes on OpenRGB 1.x's newer flag bits.
const { Client } = require('./vendor/openrgb-sdk');
// SDK protocol 4: stable since long before OpenRGB 1.0 and has everything we
// use. Protocol 5's layout changed during 1.0 development, so asking for it
// risks misreading 1.x data. OpenRGB negotiates down to this automatically.
const SDK_SETTINGS = { forceProtocolVersion: 4 };
const { renderEffect, listEffects, packRgbw, rgbwSlots } = require('./effects');
const lcd = require('./lcd');
const monsgeek = require('./monsgeek');
const hwdetect = require('./hwdetect');
const peripherals = require('./peripherals');

// Which special hardware is present on THIS PC (see hwdetect.js). Starts as
// "unknown" (null) so nothing is hidden before detection finishes.
let hw = { kraken: null, krakenBootloader: false, monsgeek: null, pawnio: null };
const hwReady = hwdetect.detect().then((h) => { hw = h; return h; }).catch(() => hw);
const krakenAllowed = () => hw.kraken !== false;

let win = null;
let client = null;
let clientSlow = null; // second connection: DRAM only (see startLoop comment)
let connected = false;
let devices = []; // cached device info from OpenRGB

// Per-device state: { enabled, effectId, params }
let deviceState = {};
let loopTimer = null;
const FPS = 30;
const startTime = Date.now();

// ---------- Kraken ring as a first-class device ----------
// The pump ring lives on our own USB driver (not OpenRGB), but the user sets it
// like any other device. Settings > Kraken pump ring:
//   synced   - every ring LED (+ anything on the Kraken's fan port) STREAMED
//              from the same effects engine and clock as every other device
//   color    - synced too, but the whole ring shows the effect's current color
//              (the older, proven 'fixed color' stream)
//   firmware - the cooler's own animations: smooth, but its own clock/speed
// The cooler only takes a few frames per second (frames sent faster are
// dropped, which looks frozen or choppy), so streaming defaults to 5 fps.
const KRAKEN_RING_ID = 'kraken-ring';
let krakenLeds = { ring: 24, fans: 0, known: false }; // filled from the driver
// 'live' (default): every ring LED streamed from the shared effects clock with
// the cooler's live-frame command (26 14, what NZXT CAM uses - see
// nzxt_kraken.py write_live), paced by its ~2 ms acknowledgement, at 30 fps
// like everything else. (The older 22/2A commands are acknowledged only after
// ~0.4 s, which capped us at ~2 updates/s.) 'smooth': the cooler runs its own
// animation with the effect's colors and speed - not in step with the rest.
const ringSynced = () => getSettings().krakenRingMode === 'live';
let ringReinit = false;
let openrgbOwnsRing = false; // OpenRGB lists the Kraken itself -> it drives the ring // set after sleep: driver re-handshakes before the next frame
function krakenRingDevice() {
  return {
    id: KRAKEN_RING_ID,
    name: 'Kraken Elite \u2014 Pump Ring' + (krakenLeds.fans ? ' + Fans' : ''),
    type: 'AIO',
    location: 'kraken-usb-ring',
    serial: 'kraken-usb-ring',
    ledCount: krakenLeds.ring + krakenLeds.fans,
    zones: [],
    kraken: true,
  };
}

// Ask the driver how many LEDs the ring and the fan port have (once).
function loadKrakenLedInfo() {
  if (krakenLeds.known || !krakenAllowed()) return;
  krakenLeds.known = true;
  Promise.resolve().then(() => lcd.lightInfo()).then((info) => {
    if (!info) return;
    const changed = info.ring !== krakenLeds.ring || info.fans !== krakenLeds.fans;
    krakenLeds = { ring: info.ring || 24, fans: info.fans || 0, known: true };
    applog('kraken lighting: ring ' + krakenLeds.ring + ' LEDs, fan port ' + krakenLeds.fans + ' LEDs');
    if (changed && connected) {
      refreshDevices().then((devs) => {
        if (win && !win.isDestroyed()) win.webContents.send('devices-updated', { devices: devs, state: deviceState });
      }).catch(() => {});
    }
  }).catch((e) => {
    applog('kraken light info failed (using 24 LEDs, retry in 30 s): ' + e.message);
    setTimeout(() => { krakenLeds.known = false; }, 30000);
  });
}

// Firmware fallback: effectId -> closest hardware ring animation.
const RING_EFFECT_MAP = {
  static:         (p) => ({ mode: 'fixed',         colors: p.colors.slice(0, 1) }),
  rainbowWave:    ()  => ({ mode: 'rainbow-flow',  colors: [] }),
  spectrumCycle:  ()  => ({ mode: 'spectrum-wave', colors: [] }),
  breathing:      (p) => ({ mode: 'breathing',     colors: p.colors.slice(0, 8) }),
  comet:          (p) => ({ mode: 'marquee-3',     colors: p.colors.slice(0, 1) }),
  sparkle:        (p) => ({ mode: 'starry-night',  colors: p.colors.slice(0, 1) }),
  colorCycle:     (p) => ({ mode: 'fading',        colors: p.colors.slice(0, 8) }),
  colorShiftWave: (p) => ({ mode: 'fading',        colors: p.colors.slice(0, 8) }),
  aurora:         (p) => ({ mode: 'fading',        colors: p.colors.slice(0, 8) }),
  heartbeat:      (p) => ({ mode: 'pulse',         colors: p.colors.slice(0, 1) }),
  wave:           (p) => ({ mode: 'pulse',         colors: p.colors.slice(0, 1) }),
  scanner:        (p) => ({ mode: 'marquee-3',     colors: p.colors.slice(0, 1) }),
  chase:          (p) => ({ mode: 'alternating-3', colors: p.colors.slice(0, 2) }),
  twinkle:        (p) => ({ mode: 'starry-night',  colors: p.colors.slice(0, 1) }),
  candle:         (p) => ({ mode: 'candle',        colors: p.colors.slice(0, 1) }),
  fire:           ()  => ({ mode: 'candle',        colors: ['#ff6a00'] }),
  gradient:       ()  => ({ mode: 'direct' }), // still: one per-LED frame (single frames work)
  off:            ()  => ({ mode: 'off',           colors: [] }),
};

const toHex = (c) => (((c.red << 16) | (c.green << 8) | c.blue) >>> 0).toString(16).padStart(6, '0');

// Synced mode: the ring has its OWN loop, paced exactly at its refresh rate
// (riding the 30 fps OpenRGB tick gave uneven 66/99 ms gaps = visible
// stutter). Each frame waits for the driver's reply before the next is sent.
let ringTimer = null;
function ringLoop() {
  ringTimer = null;
  try {
    ringFrame();
  } catch (e) {
    // Never let one bad frame end the loop for good.
    applog('kraken ring loop error: ' + e.message);
    if (!ringTimer) ringTimer = setTimeout(ringLoop, 1000);
  }
}
function ringFrame() {
  const st = deviceState[KRAKEN_RING_ID];
  if (!connected || !ringSynced() || !krakenAllowed() || !st || !devices.some((d) => d.kraken)) {
    ringTimer = setTimeout(ringLoop, 500);
    return;
  }
  loadKrakenLedInfo();
  const gap = 33; // 30 fps; the driver also waits for each frame's acknowledgement
  const started = Date.now();
  const t = (started - startTime) / 1000;
  const rec = lastSent[KRAKEN_RING_ID] || (lastSent[KRAKEN_RING_ID] = { sig: '', at: 0, fails: 0 });
  const next = (wait) => { ringTimer = setTimeout(ringLoop, Math.max(0, wait)); };
  const oneColor = false;
  let ring, fans, sig;
  try {
    if (oneColor) {
      const c = st.enabled
        ? renderEffect(st.effectId, t, 1, { ...(st.params || {}), scale: 0 }, KRAKEN_RING_ID + '|one')[0]
        : { red: 0, green: 0, blue: 0 };
      ring = [toHex(c)];
      fans = [];
    } else if (st.enabled) {
      ring = renderEffect(st.effectId, t, krakenLeds.ring, st.params, KRAKEN_RING_ID, { ring: true }).map(toHex);
      fans = krakenLeds.fans
        ? renderEffect(st.effectId, t, krakenLeds.fans, st.params, KRAKEN_RING_ID + '|fans').map(toHex)
        : [];
    } else {
      ring = new Array(krakenLeds.ring).fill('000000');
      fans = new Array(krakenLeds.fans).fill('000000');
    }
    sig = (oneColor ? 'c' : 'p') + ring.join('') + fans.join('');
  } catch (_) { return next(gap); }
  if (rec.sig === sig && started - rec.at < 2000) return next(gap); // unchanged: refresh every 2 s
  const reinit = ringReinit;
  ringReinit = false;
  const send = oneColor ? lcd.setRingDirect('fixed', ring, 2) : lcd.setLeds(ring, fans, reinit);
  send.then(() => {
    rec.sig = sig;
    rec.at = started;
    rec.fails = 0;
    next(gap - (Date.now() - started));
  }).catch((e) => {
    if (++rec.fails === 1 || rec.fails % 50 === 0) applog('kraken ring frame failed: ' + e.message);
    next(Math.min(5000, rec.fails * 500)); // back off while the driver recovers
  });
}
setTimeout(ringLoop, 1500);

// ---------- MonsGeek M1 V5 HE keyboard (own driver, stock firmware) ----------
// Streams the effect's current color into the keyboard's built-in Screen Sync
// mode on the SAME clock as every other device. Whole board = one color (per-key
// needs patched firmware), which is exactly what keeps it in sync.
const MONSGEEK_ID = 'monsgeek-he';
function monsgeekDevice() {
  return {
    id: MONSGEEK_ID,
    name: monsgeek.name(),
    type: 'Keyboard',
    location: 'monsgeek-usb',
    serial: 'monsgeek-usb',
    ledCount: 0,
    zones: [],
    monsgeek: true,
  };
}
let monsgeekLastHex = null;
function monsgeekStreamTick(now, t) {
  const st = deviceState[MONSGEEK_ID];
  if (!st || !monsgeek.isAvailable()) return;
  const rec = lastSent[MONSGEEK_ID] || (lastSent[MONSGEEK_ID] = { sig: '', at: 0 });
  if (now - rec.at < 33) return; // ~30 fps
  let hex = '000000';
  if (st.enabled) {
    let c;
    try {
      c = renderEffect(st.effectId, t, 1, { ...(st.params || {}), scale: 0 })[0];
    } catch (_) { return; }
    hex = (((c.red << 16) | (c.green << 8) | c.blue) >>> 0).toString(16).padStart(6, '0');
  }
  // Re-send an unchanged color every 2 s (self-heal after a replug/sleep).
  if (rec.sig === hex && now - rec.at < 2000) return;
  monsgeek.streamColor(hex);
  monsgeekLastHex = hex;
  rec.sig = hex;
  rec.at = now;
}

// Firmware mode only: debounced push of the ring's hardware animation. In
// synced mode ringLoop streams the ring instead.
let ringSyncTimer = null;
let lastRingSig = '';
function syncKrakenRing(force = false) {
  clearTimeout(ringSyncTimer);
  ringSyncTimer = setTimeout(async () => {
    await hwReady;
    if (!krakenAllowed() || openrgbOwnsRing) return; // no Kraken here, or OpenRGB has the ring
    const st = deviceState[KRAKEN_RING_ID];
    if (!st) return;
    if (ringSynced()) { lastRingSig = ''; return; } // the effects loop streams it
    const params = st.params || { colors: ['#ff0000'], speed: 50 };
    let spec;
    if (!st.enabled) {
      spec = { mode: 'off', colors: [] };
    } else {
      const map = RING_EFFECT_MAP[st.effectId] || RING_EFFECT_MAP.static;
      spec = map({ colors: params.colors || ['#ff0000'] });
    }
    // App speed 0-100 -> the cooler's 5 speeds (slowest..fastest).
    const sp = params.speed ?? 50;
    const speedIdx = sp < 15 ? 0 : sp < 35 ? 1 : sp < 60 ? 2 : sp < 85 ? 3 : 4;
    const brightness = (params.brightness ?? 100) / 100;
    const dim = (hex) => {
      const n = parseInt(String(hex).replace('#', ''), 16) || 0;
      const c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => Math.round(v * brightness));
      return c.map((v) => v.toString(16).padStart(2, '0')).join('');
    };
    const colors = (spec.colors || []).map(dim);
    const frame = spec.mode === 'direct'
      ? renderEffect(st.effectId, 0, krakenLeds.ring, params, KRAKEN_RING_ID, { ring: true }).map(toHex)
      : null;
    const sig = JSON.stringify([spec.mode, colors, speedIdx, frame]);
    if (!force && sig === lastRingSig) return;
    try {
      if (frame) await lcd.setLeds(frame, [], true);
      else await lcd.setRing(spec.mode, colors, speedIdx);
      lastRingSig = sig;
      if (win && !win.isDestroyed()) win.webContents.send('ring-synced', spec.mode);
    } catch (e) {
      console.error('Kraken ring sync failed:', e.message);
      if (win && !win.isDestroyed()) win.webContents.send('ring-sync-failed', e.message);
    }
  }, 600);
}

// ---------- PC sensors (CPU/GPU temps) for the LCD overlay ----------
// A PowerShell helper (bin\sensors.ps1) streams {cpu,gpu} JSON lines using
// LibreHardwareMonitor (run tools\update-lhm.ps1 once) with nvidia-smi fallback.
let pcSensors = { cpu: null, gpu: null };
let sensorsProc = null;
function sensorsScriptPath() {
  const candidates = [];
  if (process.env.PORTABLE_EXECUTABLE_DIR) {
    candidates.push(path.join(process.env.PORTABLE_EXECUTABLE_DIR, '..', 'bin', 'sensors.ps1'));
  }
  candidates.push(path.join(__dirname, '..', 'bin', 'sensors.ps1')); // npm start / dev
  if (process.resourcesPath) {
    candidates.push(path.join(process.resourcesPath, '..', '..', '..', 'bin', 'sensors.ps1')); // win-unpacked -> live
    candidates.push(path.join(process.resourcesPath, 'bin', 'sensors.ps1'));                   // bundled fallback
  }
  return candidates.find((p) => { try { return fs.existsSync(p); } catch (_) { return false; } });
}
function startSensors() {
  const script = sensorsScriptPath();
  if (!script) return;
  try {
    sensorsProc = spawn('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-Loop'],
      { stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true });
  } catch (_) { sensorsProc = null; return; }
  let buf = '';
  sensorsProc.stdout.on('data', (c) => {
    buf += c.toString();
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line.startsWith('{')) continue;
      try {
        const j = JSON.parse(line);
        pcSensors = { cpu: j.cpu ?? null, gpu: j.gpu ?? null };
      } catch (_) {}
    }
  });
  sensorsProc.on('error', () => { sensorsProc = null; });
  sensorsProc.on('exit', () => { sensorsProc = null; });
}
ipcMain.handle('sensors:get', () => pcSensors);
ipcMain.handle('hw:get', async () => { await hwReady; return hw; });
ipcMain.handle('app:openPawnio', () => shell.openExternal('https://pawnio.eu'));

// ---------- Peripherals (mouse settings stored on the mouse) ----------
const periphBackups = () => path.join(app.getPath('userData'), 'peripheral-backups');
const periphCall = async (fn) => {
  try { return { ok: true, ...(await fn()) }; } catch (e) { return { ok: false, error: e.message }; }
};
ipcMain.handle('periph:scan', () => periphCall(() => peripherals.scan()));
ipcMain.handle('periph:saveMouse', (_e, id, cfg) =>
  periphCall(() => peripherals.saveMouse(id, cfg || {}, periphBackups())));
ipcMain.handle('periph:restoreMouse', (_e, id) =>
  periphCall(() => peripherals.restoreMouse(id, periphBackups())));

// MonsGeek keyboard settings go through the keyboard's own driver process
// (it already holds the keyboard for lighting).
ipcMain.handle('periph:kbScan', () => monsgeek.request('kb_scan', { backupDir: periphBackups() }));
ipcMain.handle('periph:kbSave', (_e, want) => monsgeek.request('kb_save', { backupDir: periphBackups(), want: want || {} }, 120000));
ipcMain.handle('periph:kbRestore', () => monsgeek.request('kb_restore', { backupDir: periphBackups() }, 120000));

// Mouse buttons that open an app or website: the button sends a spare hotkey
// (Ctrl+Alt+F13...), and RGB Studio, running in the tray, catches it here.
// Opened through explorer.exe so the app starts as the normal user, not with
// RGB Studio's admin rights.
let actionAccels = [];
const ACC_MODS = ['Control', 'Shift', 'Alt', 'Super'];
function accelFor(hk) {
  if (!hk || hk.key < 0x68 || hk.key > 0x73) return null;           // F13-F24 only
  const mods = ACC_MODS.filter((_, k) => hk.mod & (1 << k));
  return [...mods, 'F' + (13 + hk.key - 0x68)].join('+');
}
function runAction(act) {
  if (!act || !act.target) return;
  try {
    const child = spawn('explorer.exe', [act.target], { detached: true, stdio: 'ignore' });
    child.on('error', (e) => applog(`action "${act.name}" failed: ${e.message}`));
    child.unref();
  } catch (e) { applog(`action "${act.name}" failed: ${e.message}`); }
}
function registerActions() {
  for (const a of actionAccels) { try { globalShortcut.unregister(a); } catch (_) {} }
  actionAccels = [];
  for (const act of (getSettings().periphActions || [])) {
    const acc = accelFor(act.hotkey);
    if (!acc || !act.target) continue;
    try {
      if (globalShortcut.register(acc, () => runAction(act))) actionAccels.push(acc);
      else applog(`hotkey ${acc} for "${act.name}" is taken by another program`);
    } catch (e) { applog(`hotkey ${acc}: ${e.message}`); }
  }
}
ipcMain.handle('periph:runAction', (_e, act) => { runAction(act); return { ok: true }; });
ipcMain.handle('periph:pickApp', async () => {
  const r = await dialog.showOpenDialog(win, {
    title: 'Choose an app, game or shortcut',
    defaultPath: 'C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs',
    filters: [{ name: 'Apps and shortcuts', extensions: ['exe', 'lnk', 'url', 'bat', 'cmd'] }, { name: 'Any file', extensions: ['*'] }],
    properties: ['openFile', 'dontAddToRecent'],
  });
  if (r.canceled || !r.filePaths.length) return { ok: false };
  const p = r.filePaths[0];
  return { ok: true, path: p, name: path.basename(p).replace(/\.(exe|lnk|url|bat|cmd)$/i, '') };
});
// Other apps whose hotkeys a mouse button can press (read live, so the button
// always matches what the app is set to).
ipcMain.handle('periph:integrations', () => {
  const out = {};
  const CV_DEFAULTS = { clip: 'F8', clip_alt: '', long_recording: 'Alt+F7', bookmark: 'F9', screenshot: 'F7' };
  try {
    const f = path.join(process.env.APPDATA || '', 'ClipVault', 'settings.json');
    if (fs.existsSync(f)) out.clipvault = { hotkeys: { ...CV_DEFAULTS, ...((JSON.parse(fs.readFileSync(f, 'utf8')) || {}).hotkeys || {}) } };
  } catch (_) {}
  if (!out.clipvault) {
    // Installed but never changed a setting: ClipVault's defaults.
    const exe = ['F:\\ClipVault\\dist\\ClipVault\\ClipVault.exe', path.join(process.env.LOCALAPPDATA || '', 'Programs', 'ClipVault', 'ClipVault.exe')];
    if (exe.some((x) => { try { return fs.existsSync(x); } catch (_) { return false; } })) out.clipvault = { hotkeys: CV_DEFAULTS, defaults: true };
  }
  return out;
});

// ---------- App settings (persisted) ----------
const settingsPath = () => path.join(app.getPath('userData'), 'settings.json');
let appSettings = null;
function getSettings() {
  if (!appSettings) {
    appSettings = {
      dramGapMs: 33,       // RAM refresh (SMBus write gap) - 30 fps, see startLoop
      gpuKeepAliveMin: 0,  // forced GPU Direct-mode re-apply interval; 0 = off
      autoStart: true,     // start (hidden in tray) when Windows logs in
      theme: 'dark',       // 'dark' | 'light'
      accent: '#7c5cff',
      krakenRingMode: 'live',   // 'live' (per LED, in sync, 30 fps) | 'smooth' (the cooler's own animation)
      krakenRingFps: 5,
    };
    try { Object.assign(appSettings, JSON.parse(fs.readFileSync(settingsPath(), 'utf8'))); } catch (_) {}
    // GPU keep-alive has no switch in the UI any more (the 2-minute check
    // re-applies Direct mode only when a card actually drops out of it).
    appSettings.gpuKeepAliveMin = 0;
    // The ring froze at 15 fps (the cooler drops frames sent that fast and
    // only shows the odd one). Move anyone still on a fast setting to 5 once.
    // Per-LED streaming freezes on this cooler's firmware (it answers Direct
    // frames with 0xFF 0x01, which looks like a refusal). The one-color stream
    // is the path that animates, so make it the default once.
    if (!appSettings.ringModeFix1) {
      appSettings.ringModeFix1 = true;
      if (appSettings.krakenRingMode !== 'firmware') appSettings.krakenRingMode = 'color';
      try { fs.writeFileSync(settingsPath(), JSON.stringify(appSettings, null, 2)); } catch (_) {}
    }
    if (!appSettings.ringModeFix2) {
      appSettings.ringModeFix2 = true;
      appSettings.krakenRingMode = 'smooth';
      try { fs.writeFileSync(settingsPath(), JSON.stringify(appSettings, null, 2)); } catch (_) {}
    }
    if (!appSettings.ringModeFix3) { // live frames now work: back to in-sync by default
      appSettings.ringModeFix3 = true;
      appSettings.krakenRingMode = 'live';
      try { fs.writeFileSync(settingsPath(), JSON.stringify(appSettings, null, 2)); } catch (_) {}
    }
    if (!appSettings.ringFpsFix1) {
      appSettings.ringFpsFix1 = true;
      if (!(appSettings.krakenRingFps <= 5)) appSettings.krakenRingFps = 5;
      try { fs.writeFileSync(settingsPath(), JSON.stringify(appSettings, null, 2)); } catch (_) {}
    }
  }
  return appSettings;
}
ipcMain.handle('settings:get', () => getSettings());
ipcMain.handle('settings:set', (_e, partial) => {
  const prev = { ...getSettings() };
  Object.assign(appSettings, partial);
  try { fs.writeFileSync(settingsPath(), JSON.stringify(appSettings, null, 2)); } catch (_) {}
  // Settings that need an action, not just a stored value:
  if ('autoStart' in partial && partial.autoStart !== prev.autoStart) { syncTask(); buildTrayMenu(); }
  if ('periphActions' in partial) registerActions();
  if ('krakenRingMode' in partial && partial.krakenRingMode !== prev.krakenRingMode) {
    delete lastSent[KRAKEN_RING_ID];
    ringReinit = true;
    syncKrakenRing(true); // firmware mode: push the animation (synced: no-op)
  }
  return { ok: true, settings: appSettings };
});
// Personal keys that must not be in the public repo live in src/local-config.json
// (git-ignored, still packed into your own builds). Settings can override them.
let localConfig = {};
try { localConfig = JSON.parse(fs.readFileSync(path.join(__dirname, 'local-config.json'), 'utf8')); } catch (_) {}
ipcMain.handle('app:info', () => ({
  giphyKey: getSettings().giphyKey || localConfig.giphyKey || '',
  version: app.getVersion(),
  logs: app.getPath('userData'),
  kraken: krakenAllowed() ? { ...krakenLeds } : null,
}));
ipcMain.handle('app:openLogs', () => shell.openPath(app.getPath('userData')));

const profilesPath = () => path.join(app.getPath('userData'), 'profiles.json');
const zonesPath = () => path.join(app.getPath('userData'), 'zones.json');

// Small rotating app log (userData\app.log) — connection attempts, OpenRGB
// start/exit, device counts. "Collect Diagnostics.bat" copies it.
function applog(msg) {
  try {
    const f = path.join(app.getPath('userData'), 'app.log');
    if (fs.existsSync(f) && fs.statSync(f).size > 1024 * 1024) fs.renameSync(f, f + '.old');
    fs.appendFileSync(f, new Date().toISOString() + ' ' + msg + '\n');
  } catch (_) {}
}

// Persisted one-time zone LED-count map: { [deviceName]: { [zoneName]: size } }.
// A personal, fixed rig only needs its zone sizes set once; we auto-restore them
// on every connect so the user never has to touch zones again.
function loadZoneMap() {
  try { return JSON.parse(fs.readFileSync(zonesPath(), 'utf8')); } catch (_) { return {}; }
}
function rememberZone(deviceName, zoneName, size) {
  const map = loadZoneMap();
  if (!map[deviceName]) map[deviceName] = {};
  map[deviceName][zoneName] = size;
  try { fs.writeFileSync(zonesPath(), JSON.stringify(map, null, 2)); } catch (_) {}
}

// Per-header LED type: { [deviceKey]: { [zoneName]: { format, physical } } }.
// format 'rgbw' | 'rgbw-rg' | 'rgbw-alt' = RGBW strip on a 3-byte ARGB header
// (see packRgbw in effects.js); physical = real LED count on the strip.
const zoneFmtPath = () => path.join(app.getPath('userData'), 'zonefmt.json');
let zoneFmtCache = null;
function loadZoneFmt() {
  if (!zoneFmtCache) {
    try { zoneFmtCache = JSON.parse(fs.readFileSync(zoneFmtPath(), 'utf8')); } catch (_) { zoneFmtCache = {}; }
  }
  return zoneFmtCache;
}
function saveZoneFmt() {
  try { fs.writeFileSync(zoneFmtPath(), JSON.stringify(zoneFmtCache || {}, null, 2)); } catch (_) {}
}

// Stable per-device key for saving/restoring zone sizes. Keying on NAME alone
// collides when two controllers share a name (e.g. two NZXT fan controllers) —
// their channels would overwrite each other. Prefer hardware id, fall back to
// name+index so the two are kept distinct.
function deviceKey(d) {
  return d.serial || d.location || (d.name + '#' + d.id);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Persist each device's effect + colors + on/off (keyed by stable device identity)
// so the whole setup — not just zone sizes — is restored automatically next launch.
const statePath = () => path.join(app.getPath('userData'), 'state.json');
// Debounced save: the Effects sliders fire dozens of updates per second, and
// each one used to read + rewrite state.json synchronously.
let saveTimer = null;
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveDeviceStates, 600);
}
function saveDeviceStates() {
  // MERGE with what's already on disk: saving while a straggler device is
  // still undetected must not erase that device's remembered settings.
  let out = {};
  try { out = JSON.parse(fs.readFileSync(statePath(), 'utf8')); } catch (_) {}
  for (const d of devices) if (deviceState[d.id]) out[deviceKey(d)] = deviceState[d.id];
  try { fs.writeFileSync(statePath(), JSON.stringify(out, null, 2)); } catch (_) {}
}
function applySavedStates() {
  // A change made in the last 600 ms (debounced save still pending) must win
  // over the file: flush it first. Before, a rescan landing in that window
  // (OpenRGB device-list updates, Kraken LED info, resizes) silently put the
  // previous effect/colors back.
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
    try { saveDeviceStates(); } catch (_) {}
  }
  let saved = {};
  try { saved = JSON.parse(fs.readFileSync(statePath(), 'utf8')); } catch (_) { return; }
  for (const d of devices) {
    const s = saved[deviceKey(d)];
    if (!s) continue;
    const cur = deviceState[d.id] || {};
    deviceState[d.id] = {
      ...cur, ...s,
      params: { ...(cur.params || {}), ...(s.params || {}) },
    };
  }
}

// OpenRGB's socket can drop (app closed, sleep, rescan) — never crash, just pause and tell the UI.
process.on('uncaughtException', (err) => {
  console.error('Uncaught:', err.message);
  if (/ECONNRESET|EPIPE|ECONNREFUSED|socket/i.test(err.message)) {
    connected = false;
    if (win && !win.isDestroyed()) win.webContents.send('conn-lost', err.message);
    scheduleReconnect(); // self-heal instead of staying dead
  }
});

// When the OpenRGB link drops, keep trying to bring it back so the lighting
// resumes on its own (otherwise devices sit on their onboard spectrum and the
// user's effects appear to "stop working"). Uses exponential backoff so a
// crashing backend doesn't get hammered. Only one pending attempt at a time.
let reconnectTimer = null;
let reconnectDelay = 4000;
function scheduleReconnect(host = '127.0.0.1', port = 6742) {
  if (reconnectTimer || connected) return;
  const attempt = () => {
    reconnectTimer = setTimeout(async () => {
      reconnectTimer = null;
      if (connected) { reconnectDelay = 4000; return; }
      try {
        const devs = await connect(host, port);
        applog('reconnected: ' + devs.length + ' devices');
        reconnectDelay = 4000;
        if (win && !win.isDestroyed()) {
          win.webContents.send('conn-restored', { devices: devs, state: deviceState });
        }
      } catch (_) {
        reconnectDelay = Math.min(Math.round(reconnectDelay * 1.7), 30000);
        attempt(); // try again later, slower each time
      }
    }, reconnectDelay);
  };
  attempt();
}

// App icon (window, taskbar, tray). ICO has crisp small sizes; PNG is the fallback.
let appIconCache = null;
function appIcon() {
  if (appIconCache) return appIconCache;
  let img = nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon.ico'));
  if (img.isEmpty()) {
    try { img = nativeImage.createFromBuffer(fs.readFileSync(path.join(__dirname, 'assets', 'icon.png'))); } catch (_) {}
  }
  appIconCache = img;
  return img;
}

function showWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

// Window chrome blends into the app: no native title bar, just Windows' own
// min/max/close drawn over our top bar in the current theme's background.
const THEME_BG = { ynzie: '#0b0b10', kara: '#0c0911', kaleb: '#0b0909', james: '#080c0a', dnarte: '#07090f', medal: '#0a0b08' };
const TITLEBAR_H = 36;
const titleBarFor = (bg) => ({ color: bg, symbolColor: '#c9c8d3', height: TITLEBAR_H });
ipcMain.handle('app:titlebar', (_e, bg) => {
  if (!/^#[0-9a-f]{6}$/i.test(bg || '') || !win || win.isDestroyed()) return;
  try { win.setTitleBarOverlay(titleBarFor(bg)); win.setBackgroundColor(bg); } catch (_) {}
});

function createWindow() {
  // Started by the logon task (--minimized)? Stay in the tray - unless the
  // user just clicked the app and it was handed to the task (launcher.js).
  const startMinimized = process.argv.includes('--minimized') && !launcher.consumeShowRequest();
  win = new BrowserWindow({
    width: 1280,
    height: 820,
    backgroundColor: THEME_BG[getSettings().theme] || THEME_BG.ynzie,
    titleBarStyle: 'hidden',
    titleBarOverlay: titleBarFor(THEME_BG[getSettings().theme] || THEME_BG.ynzie),
    icon: appIcon(),
    show: !startMinimized,           // don't flash the window on boot
    minimizable: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.setMenuBarVisibility(false);
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  // Behave like a background tray app: minimizing or closing hides to the tray
  // instead of quitting. Real quit only happens from the tray menu.
  win.on('minimize', (e) => { e.preventDefault(); win.hide(); });
  win.on('close', (e) => {
    if (!app.isQuitting) { e.preventDefault(); win.hide(); }
  });
  // Started from the login task with --minimized? Stay hidden in the tray.
  if (startMinimized) win.once('ready-to-show', () => win.hide());
}

let tray = null;
function buildTrayMenu() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open RGB Studio', click: showWindow },
    { type: 'separator' },
    {
      label: 'Start with Windows',
      type: 'checkbox',
      checked: getSettings().autoStart !== false,
      click: (item) => setAutoStart(item.checked),
    },
    { type: 'separator' },
    { label: 'Quit RGB Studio', click: () => { app.isQuitting = true; app.quit(); } },
  ]));
}
function createTray() {
  if (tray) return;
  tray = new Tray(appIcon());
  tray.setToolTip('RGB Studio');
  buildTrayMenu();
  tray.on('click', () => {
    if (!win) return;
    if (win.isVisible() && !win.isMinimized()) win.hide();
    else showWindow();
  });
}

// Keeps the "RGB Studio" scheduled task pointed at this exe (prompt-free
// launches) and its logon trigger matching "Start with Windows".
function syncTask() {
  launcher.syncLogonTask(getSettings().autoStart !== false, (err, out) => {
    applog('logon task: ' + (err ? 'FAILED ' + err.message : out));
  });
}
function setAutoStart(on) {
  getSettings().autoStart = !!on;
  try { fs.writeFileSync(settingsPath(), JSON.stringify(appSettings, null, 2)); } catch (_) {}
  syncTask();
  buildTrayMenu();
}

// Opening the app again while it runs (Start menu, desktop, task) just brings
// the running window forward.
app.on('second-instance', () => {
  launcher.consumeShowRequest(); // clear the hand-off flag, if any
  showWindow();
});

// ---------- Bundled OpenRGB (auto-launched, hidden) ----------

let openrgbProc = null;

// "Is an OpenRGB server there?" — done as a REAL SDK hello (magic header +
// protocol-version request), then a clean close. The old check opened a TCP
// connection and dropped it without a word; OpenRGB 1.1 logs that as
// "recv_select failed receiving magic, closing listener" and resets its
// connection handler, so the app's real connect right after kept failing.
function portOpen(host, port) {
  return new Promise((resolve) => {
    const s = net.connect({ host, port });
    let settled = false;
    const done = (v) => {
      if (settled) return;
      settled = true;
      try { s.end(); } catch (_) {}
      setTimeout(() => { try { s.destroy(); } catch (_) {} }, 200);
      resolve(v);
    };
    s.setTimeout(1500, () => done(s.readyState === 'open')); // connected but silent = up
    s.on('connect', () => {
      const hdr = Buffer.alloc(16);
      hdr.write('ORGB', 0, 'ascii');
      hdr.writeUInt32LE(0, 4);   // device id
      hdr.writeUInt32LE(40, 8);  // NET_PACKET_ID_REQUEST_PROTOCOL_VERSION
      hdr.writeUInt32LE(4, 12);  // payload length
      const ver = Buffer.alloc(4);
      ver.writeUInt32LE(5, 0);   // our protocol version (same as openrgb-sdk)
      s.write(Buffer.concat([hdr, ver]));
    });
    s.on('data', () => done(true)); // any reply = a live OpenRGB server
    s.on('error', () => done(false));
  });
}

function findOpenRGBExe() {
  const candidates = [
    // bundled next to the packaged exe
    path.join(process.resourcesPath || '', 'bin', 'OpenRGB', 'OpenRGB.exe'),
    // bundled in the project folder (npm start)
    path.join(__dirname, '..', 'bin', 'OpenRGB', 'OpenRGB.exe'),
    // system installs, just in case
    'C:\\Program Files\\OpenRGB\\OpenRGB.exe',
    'C:\\Program Files (x86)\\OpenRGB\\OpenRGB.exe',
  ];
  return candidates.find((p) => { try { return fs.existsSync(p); } catch (_) { return false; } });
}

// Start OpenRGB as a hidden background server if nothing is listening yet.
let lastSpawn = 0;
// Turn one OpenRGB device detector on/off in its config (read at startup).
function setOpenRGBDetector(cfgDir, name, on) {
  const f = path.join(cfgDir, 'OpenRGB.json');
  try {
    let j = {};
    try { j = JSON.parse(fs.readFileSync(f, 'utf8')); } catch (_) {}
    j.Detectors = j.Detectors || {};
    j.Detectors.detectors = j.Detectors.detectors || {};
    if (j.Detectors.detectors[name] === on) return;
    j.Detectors.detectors[name] = on;
    fs.writeFileSync(f, JSON.stringify(j, null, 4));
    applog(`OpenRGB detector "${name}" ${on ? 'enabled' : 'disabled'}`);
  } catch (e) {
    applog('could not update OpenRGB detectors: ' + e.message);
  }
}

let openrgbNeedsGui = null; // set from settings on first use (see below)

async function ensureOpenRGB(host, port) {
  if (openrgbNeedsGui === null) openrgbNeedsGui = !!getSettings().openrgbNeedsGui;
  if (await portOpen(host, port)) return true;
  const exe = findOpenRGBExe();
  if (!exe) {
    throw new Error(
      'OpenRGB backend not found. Run "Build RGB Studio.bat" - it downloads it into the bin folder.'
    );
  }
  // Don't respawn faster than every 20s. If OpenRGB is crashing on some hardware,
  // hammering it with restarts just produces a storm of flicker and re-pops its
  // zone dialog — better to wait and let the reconnect backoff pace things.
  if (Date.now() - lastSpawn < 20000) {
    throw new Error('OpenRGB backend not ready yet — waiting before restarting it.');
  }
  lastSpawn = Date.now();
  // Pin OpenRGB to a permanent config directory. The portable build otherwise
  // hands it a throwaway folder each launch, so its saved zone sizes and the
  // "Do not show again" choice for the resize-zones dialog never persist — which
  // is why that popup came back every single time. With a stable --config dir,
  // dismissing it once makes it stay gone.
  const cfgDir = path.join(app.getPath('userData'), 'openrgb-config');
  try { fs.mkdirSync(cfgDir, { recursive: true }); } catch (_) {}
  // The Kraken 2024 Elite's ring freezes when streamed per LED - OpenRGB's
  // way of driving it (its own bug tracker: animates only at ~1 update/s).
  // RGB Studio drives that ring itself, so OpenRGB must leave it alone.
  await hwReady;
  if (hw.kraken) setOpenRGBDetector(cfgDir, 'NZXT Kraken 2024 ELITE Series RGB', false);
  applog('starting OpenRGB: ' + exe);
  let exitInfo = null;
  // windowsHide: never borrow a console window (OpenRGB 1.1 prints its log to
  // whatever console it inherits, e.g. the build window).
  // Headless: '--server' without '--startminimized' (which implies the GUI)
  // runs only the SDK server - no window and no OpenRGB tray icon. RGB Studio
  // is the only thing the user sees. '--noautoconnect' stops it from trying
  // to join some other OpenRGB server as a client.
  const headless = !openrgbNeedsGui;
  const args = headless
    ? ['--config', cfgDir, '--server', '--noautoconnect']
    : ['--config', cfgDir, '--server', '--startminimized']; // fallback: tray icon, but works
  const spawnedAt = Date.now();
  openrgbProc = spawn(exe, args, { stdio: 'ignore', windowsHide: true });
  openrgbProc.on('error', (e) => { exitInfo = 'failed to start: ' + e.message; openrgbProc = null; });
  openrgbProc.on('exit', (code) => {
    exitInfo = 'exited with code ' + code;
    applog('OpenRGB ' + exitInfo);
    openrgbProc = null;
    // Safety net: if this build quits right away without its window, use the
    // minimized-to-tray start from now on (and remember it).
    if (headless && Date.now() - spawnedAt < 15000) {
      openrgbNeedsGui = true;
      getSettings().openrgbNeedsGui = true;
      try { fs.writeFileSync(settingsPath(), JSON.stringify(appSettings, null, 2)); } catch (_) {}
      applog('OpenRGB headless start failed - using the minimized start instead');
    }
  });
  // OpenRGB 1.x can take a while to finish device detection on first start.
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    if (await portOpen(host, port)) { applog('OpenRGB server up after ' + (i + 1) + ' s'); return true; }
    if (exitInfo) throw new Error('OpenRGB backend ' + exitInfo + ' (it crashed or refused to start).');
  }
  throw new Error('OpenRGB backend started but its server did not come up within 60 s.');
}

// ---------- OpenRGB ----------

let connecting = false;
async function connect(host = '127.0.0.1', port = 6742) {
  if (connecting) return devices; // never run two connects at once
  connecting = true;
  try {
    await hwReady; // know which special hardware exists before building the list
    await ensureOpenRGB(host, port);
    if (client) {
      // Detach first, so the old connection's "disconnect" event (which we
      // cause ourselves here) isn't mistaken for a lost connection.
      const old = client;
      client = null;
      connected = false;
      try { await old.disconnect(); } catch (_) {}
    }
    if (clientSlow) {
      try { await clientSlow.disconnect(); } catch (_) {}
      clientSlow = null;
    }
    client = new Client('RGB Studio', port, host, SDK_SETTINGS);
    await client.connect();
    applog('SDK protocol in use: ' + client.protocolVersion);
    watchClient(client, host, port);
    // Slow lane: OpenRGB runs each client connection on its own server thread,
    // and a slow SMBus (DRAM) write blocks everything queued behind it on the
    // SAME connection. Giving DRAM its own connection keeps fans/GPU/mobo
    // packets flowing at full rate.
    try {
      clientSlow = new Client('RGB Studio (slow lane)', port, host, SDK_SETTINGS);
      await clientSlow.connect();
    } catch (_) { clientSlow = null; }
    connected = true;
    connErrCount = 0;
    zonesApplied.clear(); // new connection: re-check every device's saved sizes once
    await readControllers();
    await applySavedZones(); // devices present now; late ones via refreshDevices()
    applySavedStates();      // restore saved effects/colors/on-off
    syncKrakenRing();        // push restored ring state to the cooler firmware
    startLoop();
    scheduleRescans(); // OpenRGB detects devices gradually — pick up stragglers
    return devices;
  } finally {
    connecting = false;
  }
}

// OpenRGB 1.x starts its server BEFORE device detection finishes, then
// announces each newly found device with a "device list updated" message (also
// on hot-plug). Refresh on those (debounced), so devices found after the first
// connect show up within ~1.5 s instead of being missed.
// Also handle the connection dropping, for the CURRENT client only (old
// clients we closed on purpose during a reconnect must not trigger another).
let listUpdateTimer = null;
function watchClient(c, host, port) {
  if (typeof c.on !== 'function') return;
  c.on('deviceListUpdated', () => {
    if (c !== client) return;
    clearTimeout(listUpdateTimer);
    listUpdateTimer = setTimeout(async () => {
      if (!connected || c !== client) return;
      try {
        const devs = await refreshDevices();
        applog('device list updated by OpenRGB: ' + devs.length + ' devices');
        if (win && !win.isDestroyed()) win.webContents.send('devices-updated', { devices: devs, state: deviceState });
      } catch (e) { applog('refresh after device-list update failed: ' + e.message); }
    }, 1500);
  });
  const lost = (why) => {
    if (c !== client || !connected) return;
    connected = false;
    applog('OpenRGB connection lost: ' + why);
    if (win && !win.isDestroyed()) win.webContents.send('conn-lost', why);
    scheduleReconnect(host, port);
  };
  c.on('disconnect', () => lost('disconnected'));
  c.on('error', (e) => lost((e && e.message) || 'socket error'));
}

// OpenRGB's device detection runs for a while after startup; rescan a couple of
// times to pick up stragglers. Timers are tracked and cleared on every (re)connect
// so repeated reconnects can't stack dozens of overlapping rescans.
let rescanTimers = [];
function scheduleRescans() {
  for (const id of rescanTimers) clearTimeout(id);
  rescanTimers = [];
  for (const ms of [5000, 15000]) {
    rescanTimers.push(setTimeout(async () => {
      if (!connected) return;
      try {
        const before = devices.length;
        const devs = await refreshDevices();
        if (devs.length !== before && win && !win.isDestroyed()) {
          win.webContents.send('devices-updated', { devices: devs, state: deviceState });
        }
      } catch (e) { applog('rescan failed: ' + e.message); }
    }, ms));
  }
}

// Read every controller's current data into the `devices` cache and force Direct mode.
//
// Scans are SERIALIZED: rescan timers, the MonsGeek hot-plug callback, resume,
// the Rescan button and zone resizes can all trigger one, and two scans
// interleaving used to mix their device lists. Each scan also builds into a
// local list and swaps it in at the end, so the effects loop never sees a
// half-built (or briefly empty) device list.
let scanChain = Promise.resolve();
function readControllers() {
  const run = scanChain.then(readControllersImpl, readControllersImpl);
  scanChain = run.catch(() => {});
  return run;
}

async function readControllersImpl() {
  const count = await client.getControllerCount();
  const list = [];
  for (let i = 0; i < count; i++) {
    // One unreadable device must not take down the whole list (that's what
    // froze everything when the SDK couldn't parse OpenRGB 1.1's data).
    let d;
    try {
      d = await client.getControllerData(i);
    } catch (e) {
      applog('skipped device ' + i + ' (could not read its data): ' + e.message);
      continue;
    }
    // Dedupe zones by name. OpenRGB can transiently report a device's zones
    // several times right over a resize (NZXT/Kraken especially) — real channels
    // have unique names, so keeping the first of each name drops only the glitchy
    // repeats. This is what a manual Rescan was doing for you; now it's automatic.
    // The Kraken 2024 Elite has an OpenRGB bug where resizing its zone makes it
    // duplicate LEDs without bound (the "LED count climbs forever"). Its ring is
    // already detected correctly and never needs sizing, so we mark its zones
    // non-resizable — that hides the Set boxes AND excludes it from auto-set and
    // saved-zone restore, so the runaway can never be triggered.
    const resizeUnsafe = /kraken/i.test(d.name || '');
    const zseen = new Set();
    const zones = [];
    const fmtMap = loadZoneFmt()[d.serial || d.location || (d.name + '#' + i)] || {};
    let ledOffset = 0; // where each zone's LEDs start in the device's LED list
    (d.zones || []).forEach((z, zi) => {
      const start = ledOffset;
      ledOffset += z.ledsCount || 0;
      if (zseen.has(z.name)) return;
      zseen.add(z.name);
      zones.push({
        id: zi,
        name: z.name,
        start,
        ledsCount: z.ledsCount,
        ledsMin: z.ledsMin ?? 0,
        ledsMax: z.ledsMax ?? 0,
        resizable: !resizeUnsafe && ((z.ledsMin ?? 0) !== (z.ledsMax ?? 0)),
        fmt: fmtMap[z.name] || null, // RGBW strip config, if any
      });
    });
    const directModeIdx = (d.modes || []).findIndex((m) => /direct/i.test(m.name));
    list.push({
      id: i,
      name: d.name,
      type: d.type,
      // Stable hardware identity (USB path / serial). Two physically separate
      // controllers with the same name differ here; a phantom re-enumeration of
      // one device matches here. Used to dedupe safely without merging real ones.
      location: d.location || '',
      serial: d.serial || '',
      ledCount: d.leds.length,
      directModeIdx, // remembered so we can re-assert Direct mode later
      zones,
    });
    if (!deviceState[i]) deviceState[i] = currentLook();
    // Put the device in Direct mode (by name) so SDK updates actually show.
    // Only switch if it ISN'T already in Direct — re-asserting the mode on every
    // rescan fires a pile of slow calls that briefly freeze every device.
    try {
      const activeIdx = (d.activeMode ?? d.active_mode ?? -1);
      if (directModeIdx >= 0) {
        if (activeIdx !== directModeIdx) await client.updateMode(i, directModeIdx);
      } else {
        await client.setCustomMode(i);
      }
    } catch (_) {}
  }
  // Collapse only TRUE duplicates — same physical device enumerated twice (which
  // OpenRGB can do right after a zone resize). We key on hardware identity
  // (location/serial), NOT name, so two separate controllers that happen to share
  // a name (e.g. two NZXT fan controllers) are both kept. If a device exposes no
  // stable id, we keep it rather than risk merging real hardware.
  const seen = new Set();
  const deduped = [];
  for (const dev of list) {
    const key = dev.serial || dev.location;
    if (key) {
      if (seen.has(key)) continue; // genuine duplicate of the same physical device
      seen.add(key);
    }
    deduped.push(dev);
  }
  // The Kraken ring rides on our own USB driver, but it belongs in the same
  // device list so one "Apply to all" covers it too. Only on PCs that
  // actually have a Kraken.
  // If OpenRGB itself drives the Kraken (its command interface is back on the
  // Windows HID driver), the ring is a normal OpenRGB device - don't add ours
  // on top, or two programs would fight over it.
  openrgbOwnsRing = deduped.some((d) => /kraken/i.test(d.name || ''));
  if (krakenAllowed() && !openrgbOwnsRing) {
    deduped.push(krakenRingDevice());
    if (!deviceState[KRAKEN_RING_ID]) deviceState[KRAKEN_RING_ID] = currentLook();
  }
  // MonsGeek keyboard: only listed while its driver actually has it open.
  if (monsgeek.isAvailable()) {
    deduped.push(monsgeekDevice());
    if (!deviceState[MONSGEEK_ID]) {
      // First time we see the keyboard: inherit the CURRENT look (the ring
      // follows "Apply to all") instead of defaulting to rainbow.
      deviceState[MONSGEEK_ID] = currentLook();
    }
  }
  devices = deduped; // swap in the finished list in one step
  // CRITICAL: OpenRGB detects devices gradually, and every (re)scan lands here.
  // Devices that appear late (NZXT controllers, DRAM) used to get factory
  // defaults because saved states were only applied once at connect — that's
  // why the setup had to be redone every launch. Re-apply after EVERY scan.
  applySavedStates();
  return devices;
}

// A plain refresh just re-reads the current device state. It does NOT resize
// anything — resizing on every rescan was making OpenRGB spawn duplicate devices.
async function refreshDevices() {
  await readControllers();
  await applySavedZones(); // restores sizes for devices seen for the first time
  return devices;
}

// ---------- Direct-mode keeper ----------
// GPUs (the 9070 XT especially) can fall out of Direct mode after sleep,
// screen-off, or a graphics driver reset and revert to onboard rainbow.
//
// BUT re-sending the mode is NOT free: OpenRGB's UpdateMode re-initialises
// the controller, and many GPU / motherboard lighting chips blank or blink
// their LEDs when that happens. The old watchdog forced it on every GPU every
// 2 minutes — that was the periodic "GPU flashing". Now:
//  - Routine check (every 2 min): only re-applies Direct if OpenRGB reports the
//    device is NOT in Direct. A device already in Direct is never touched.
//  - Forced re-apply happens only where it's actually needed: after resume /
//    unlock (when cards really do drop out), and optionally on a slow
//    user-chosen "GPU keep-alive" timer (Devices page, default Off).
function isGpu(d) {
  return d.type === 2 /* OpenRGB DEVICE_TYPE_GPU */
    || /\bgpu\b|radeon|geforce|\brtx\b|\brx ?\d{4}|\barc a\d/i.test(d.name || '');
}

let reasserting = false;
async function reassertDirect({ force = false, gpuOnly = false } = {}) {
  if (!connected || !client || reasserting) return;
  reasserting = true;
  try {
    for (const d of devices) {
      if (d.kraken || d.monsgeek || d.directModeIdx == null || d.directModeIdx < 0) continue;
      if (gpuOnly && !isGpu(d)) continue;
      try {
        if (!force) {
          const cur = await client.getControllerData(d.id);
          if ((cur.activeMode ?? -1) === d.directModeIdx) continue; // already Direct
        }
        await client.updateMode(d.id, d.directModeIdx);
        if (lastSent[d.id]) lastSent[d.id].sig = ''; // re-push the current frame
      } catch (_) {}
    }
  } finally {
    reasserting = false;
  }
}

function reassertAfterResume() {
  setTimeout(async () => {
    if (!connected) return;
    try {
      await refreshDevices();
      await reassertDirect({ force: true }); // one deliberate re-apply after wake
      lastSent = {};                         // re-push current frames to everything
      ringReinit = true;                     // synced ring: re-handshake after wake
      syncKrakenRing(true);                  // firmware mode: re-send the animation
    } catch (_) {}
  }, 8000); // give USB/driver stack time to settle after wake
}

// Routine, non-flashing check.
setInterval(() => { reassertDirect({ force: false }); }, 120000);

// Optional forced GPU keep-alive (minutes; 0 = off). Checked every minute so a
// settings change takes effect without a restart.
let lastKeepAlive = Date.now();
setInterval(() => {
  const mins = +getSettings().gpuKeepAliveMin || 0;
  if (!mins) return;
  if (Date.now() - lastKeepAlive < mins * 60000) return;
  lastKeepAlive = Date.now();
  reassertDirect({ force: true, gpuOnly: true });
}, 60000);

// Apply the saved one-time zone sizes (e.g. JRAINBOW2 = 40). This is the ONLY
// automatic place we resize. It runs ONCE PER DEVICE PER CONNECTION — the
// first time each device is seen, including devices OpenRGB only finds a few
// seconds after we connect (it used to run once at connect, which silently
// skipped late devices like the motherboard; OpenRGB's own sizes file used to
// hide that). Never repeated for the same device, so it can't feed the
// duplicate-device loop. Zones with no saved value just remember their size.
const zonesApplied = new Set(); // deviceKeys handled this connection
async function applySavedZones() {
  const zmap = loadZoneMap();
  const resized = new Set();
  for (const d of devices) {
    const key = deviceKey(d);
    if (zonesApplied.has(key)) continue;
    zonesApplied.add(key);
    for (const z of d.zones) {
      if (!z.resizable) continue;
      const saved = zmap[key] ? zmap[key][z.name] : undefined;
      if (saved == null) {
        rememberZone(key, z.name, z.ledsCount); // remember detected size, no resize
      } else if (saved !== z.ledsCount) {
        try { await client.resizeZone(d.id, z.id, saved); resized.add(key); } catch (_) {}
      }
    }
  }
  if (resized.size) {
    await sleep(600);         // let OpenRGB settle first
    await readControllers();
    await reapplyDirectAfterResize(resized);
  }
}

// After a header is resized, the controller keeps its OLD LED layout until the
// mode is sent again: MSI Mystic Light & co. carry each header's LED count in
// the mode packet. Without this the new LEDs show a jumbled mess. One re-send,
// only for the resized device(s).
async function reapplyDirectAfterResize(keys) {
  for (const d of devices) {
    if (!keys.has(deviceKey(d)) || d.directModeIdx == null || d.directModeIdx < 0) continue;
    try {
      await client.updateMode(d.id, d.directModeIdx);
      applog('re-applied Direct mode after resize: ' + d.name);
    } catch (_) {}
    if (lastSent[d.id]) lastSent[d.id].sig = ''; // push the current frame right away
  }
}

// Cheap fingerprint of a frame so we can skip re-sending identical colors.
function colorsSignature(colors) {
  let s = '';
  for (const c of colors) s += (((c.red << 16) | (c.green << 8) | c.blue) >>> 0).toString(36) + ',';
  return s;
}

// Per-device record of the last frame we actually pushed: { sig, at }.
let lastSent = {};
// Consecutive connection-error frames — used to ride out brief blips instead of
// tearing the whole connection down (and triggering a reconnect) on one hiccup.
let connErrCount = 0;

// One device's frame. Each zone (fan, strip, header, ring) gets the WHOLE
// effect sized to its own LED count, so every fan/strip shows the complete
// pattern instead of a slice of one long device-wide strip. RGBW strips are
// rendered for their PHYSICAL LEDs and re-packed into 3-byte header slots.
function renderDevice(d, st, t) {
  const key = deviceKey(d);
  const zs = d.zones.filter((z) => z.ledsCount > 0 && z.start + z.ledsCount <= d.ledCount);
  const covered = zs.reduce((a, z) => a + z.ledsCount, 0);
  const colors = (zs.length && covered === d.ledCount)
    ? new Array(d.ledCount)
    : renderEffect(st.effectId, t, d.ledCount, st.params, key);
  for (const z of zs) {
    const zkey = key + '|' + z.name;
    const opts = { ring: /fan|ring/i.test(z.name) }; // circular: mirror Fire/Gradient
    const part = (z.fmt && z.fmt.physical)
      ? packRgbw(renderEffect(st.effectId, t, z.fmt.physical, st.params, zkey, opts), z.ledsCount, z.fmt.format)
      : renderEffect(st.effectId, t, z.ledsCount, st.params, zkey, opts);
    for (let k = 0; k < z.ledsCount; k++) colors[z.start + k] = part[k] || { red: 0, green: 0, blue: 0 };
  }
  return colors;
}

function startLoop() {
  if (loopTimer) clearInterval(loopTimer);
  lastSent = {};
  loopTimer = setInterval(() => {
    if (!connected) return;
    const now = Date.now();
    const t = (now - startTime) / 1000;
    for (const d of devices) {
      if (d.kraken) continue; // ring: its own paced loop (ringLoop)
      if (d.monsgeek) { monsgeekStreamTick(now, t); continue; } // keyboard: streamed color
      const st = deviceState[d.id];
      if (!st || !st.enabled) continue;

      // Per-device pacing. NZXT firmware needs breathing room between writes
      // or it silently drops them; DRAM is written over slow SMBus. Everything
      // else runs at the full 30fps tick.
      const nzxt = /nzxt|hue|kraken/i.test(d.name);
      const dram = /trident|g\.?skill|dram|ddr\d|vengeance|dominator|ripjaws|fury|ballistix/i.test(d.name);
      // RAM refresh is a setting (Settings page, default 30 fps): no reports
      // tie the update rate to freezes - SMBus trouble comes from other tools
      // (HWiNFO etc.) using the bus at the same time. Per-device override
      // (st.fpsGap, device card > Advanced) wins over the heuristics.
      // Kraken ring via OpenRGB: 10 fps to start - this cooler is known to
      // drop frames sent faster (per-device Refresh rate can raise it).
      const autoGap = dram ? getSettings().dramGapMs : /kraken/i.test(d.name) ? 100 : (nzxt ? 50 : 33);
      const minGap = st.fpsGap || autoGap;
      const rec = lastSent[d.id] || (lastSent[d.id] = { sig: '', at: 0 });
      if (now - rec.at < minGap) continue;

      let colors;
      try {
        colors = renderDevice(d, st, t);
      } catch (_) { continue; }

      // Skip identical frames — BUT re-send the current frame every 2 s.
      // Flaky controllers occasionally DROP a write; without this refresh, a
      // dropped static color would never be retried and the strip stays wrong.
      const sig = colorsSignature(colors);
      if (rec.sig === sig && now - rec.at < 2000) continue;

      // NOTE: updateLeds is SYNCHRONOUS in openrgb-sdk (plain socket.write,
      // returns void). It never blocks on the hardware, so no async juggling —
      // just guard against a dead socket throwing.
      try {
        // Slow devices ride the second connection so their sluggish writes
        // can't delay the fast ones queued on the main connection.
        const useSlow = clientSlow && (dram || minGap >= 125);
        (useSlow ? clientSlow : client).updateLeds(d.id, colors);
        rec.sig = sig;
        rec.at = now;
        connErrCount = 0; // a good write clears the blip counter
      } catch (e) {
        if (/ECONNRESET|EPIPE|ECONNREFUSED|socket|not connected|write after end/i.test(e.message || '')) {
          // Ride out short blips; only tear down + reconnect after a sustained
          // run of connection errors, so one hiccup doesn't cause a storm.
          if (++connErrCount < 10) continue;
          connErrCount = 0;
          connected = false;
          if (win && !win.isDestroyed()) win.webContents.send('conn-lost', e.message);
          scheduleReconnect();
          return;
        }
        // Otherwise a single device just hiccuped — ignore this frame.
      }
    }
  }, Math.round(1000 / FPS));
}

// ---------- IPC ----------

ipcMain.handle('openrgb:connect', async (_e, host, port) => {
  try {
    const devs = await connect(host, port);
    applog('connected: ' + devs.length + ' devices: ' + devs.map((d) => d.name).join(' | '));
    return { ok: true, devices: devs, state: deviceState };
  } catch (e) {
    connected = false;
    applog('connect failed: ' + e.message);
    // Keep trying in the background. Before, a failed FIRST connect (e.g. the
    // backend still starting at boot) left the app dead until a manual click.
    scheduleReconnect(host, port);
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('openrgb:refresh', async () => {
  if (!connected) return { ok: false, error: 'Not connected' };
  try {
    return { ok: true, devices: await refreshDevices(), state: deviceState };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('effects:list', () => listEffects());

// Live preview for the Effects page: render one frame of an effect for a
// virtual strip so the user can SEE what speed/scale do before applying.
ipcMain.handle('device:setState', (_e, deviceId, partial) => {
  const st = deviceState[deviceId];
  if (!st) return { ok: false, error: 'Unknown device' };
  Object.assign(st, partial);
  if (partial.params) st.params = { ...st.params, ...partial.params };
  scheduleSave(); // remember effect/color choices for next launch
  if (deviceId === KRAKEN_RING_ID) syncKrakenRing();
  return { ok: true };
});

// Set how many LEDs are on a zone (fans/strips plugged into controllers or ARGB headers).
// Persist it so this fixed rig never needs the zone set again.
ipcMain.handle('device:resizeZone', async (_e, deviceId, zoneId, size) => {
  if (!connected) return { ok: false, error: 'Not connected' };
  try {
    const dev = devices.find((x) => x.id === deviceId);
    const zone = dev && dev.zones.find((z) => z.id === zoneId);
    await client.resizeZone(deviceId, zoneId, size);
    if (dev && zone) rememberZone(deviceKey(dev), zone.name, size);
    await sleep(500); // let OpenRGB settle so we don't read duplicated zones
    const devs = await refreshDevices();
    if (dev) await reapplyDirectAfterResize(new Set([deviceKey(dev)]));
    return { ok: true, devices: devs, state: deviceState };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

// LED type of a header. format 'rgb' = normal (clears the setting); 'rgbw',
// 'rgbw-rg', 'rgbw-alt' = RGBW strip with `physical` real LEDs: the header is
// resized to the ceil(4*N/3) 3-byte slots the packed RGBW stream needs.
ipcMain.handle('zone:setFormat', async (_e, deviceId, zoneId, format, physical) => {
  if (!connected) return { ok: false, error: 'Not connected' };
  try {
    const dev = devices.find((x) => x.id === deviceId);
    const zone = dev && dev.zones.find((z) => z.id === zoneId);
    if (!dev || !zone) return { ok: false, error: 'Unknown device/zone' };
    const key = deviceKey(dev);
    const map = loadZoneFmt();
    physical = Math.max(1, Math.round(+physical || 0));
    let size;
    if (format === 'rgb') {
      if (map[key]) delete map[key][zone.name];
      size = physical;
    } else {
      if (!['rgbw', 'rgbw-rg', 'rgbw-alt'].includes(format)) return { ok: false, error: 'Unknown LED type' };
      (map[key] = map[key] || {})[zone.name] = { format, physical };
      size = rgbwSlots(physical);
    }
    saveZoneFmt();
    if (zone.ledsMax && size > zone.ledsMax) size = zone.ledsMax;
    if (size !== zone.ledsCount) {
      await client.resizeZone(deviceId, zoneId, size);
      await sleep(500);
    }
    rememberZone(key, zone.name, size);
    const devs = await refreshDevices();
    await reapplyDirectAfterResize(new Set([key]));
    applog(`zone format: ${dev.name} / ${zone.name} = ${format}, ${physical} LEDs -> ${size} slots`);
    return { ok: true, devices: devs, state: deviceState };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

// One-click auto-set: size every resizable zone and remember it, so all LEDs
// light up without the user learning what a "zone" is. mode 'max' fills each
// zone to its maximum (lights everything); 'detected' keeps OpenRGB's detected
// count where it found one, else falls back to max.
ipcMain.handle('zones:autoSet', async (_e, mode = 'max') => {
  if (!connected) return { ok: false, error: 'Not connected' };
  try {
    let touched = 0;
    const resized = new Set();
    for (const d of devices) {
      const key = deviceKey(d);
      for (const z of d.zones) {
        if (!z.resizable) continue;
        const size = mode === 'detected'
          ? (z.ledsCount > 0 ? z.ledsCount : z.ledsMax)
          : z.ledsMax;
        if (!size || size <= 0) continue;
        if (size !== z.ledsCount) {
          try { await client.resizeZone(d.id, z.id, size); resized.add(key); } catch (_) {}
        }
        rememberZone(key, z.name, size);
        touched++;
      }
    }
    await sleep(700); // let OpenRGB settle so we don't read duplicated zones
    const devs = await refreshDevices();
    if (resized.size) await reapplyDirectAfterResize(resized);
    return { ok: true, touched, devices: devs, state: deviceState };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

// What a device that shows up for the first time should do: the last look
// applied with "Apply to all" (or the most common one right now) - not a
// rainbow default that doesn't match anything else.
function currentLook() {
  const s = getSettings().lastLook;
  if (s && s.effectId) {
    return { enabled: true, effectId: s.effectId, params: JSON.parse(JSON.stringify(s.params || {})) };
  }
  const counts = {};
  let best = null;
  for (const st of Object.values(deviceState)) {
    if (!st || !st.effectId) continue;
    counts[st.effectId] = (counts[st.effectId] || 0) + 1;
    if (!best || counts[st.effectId] > counts[best.effectId]) best = st;
  }
  return best
    ? { enabled: true, effectId: best.effectId, params: JSON.parse(JSON.stringify(best.params || {})) }
    : { enabled: true, effectId: 'rainbowWave', params: { speed: 50, brightness: 100, scale: 50, colors: ['#ff0000'] } };
}

ipcMain.handle('device:applyAll', (_e, partial) => {
  for (const id of Object.keys(deviceState)) {
    const st = deviceState[id];
    Object.assign(st, { effectId: partial.effectId ?? st.effectId });
    if (partial.params) st.params = { ...st.params, ...partial.params };
  }
  scheduleSave();   // remember effect/color choices for next launch
  // ...including devices that aren't listed this moment (e.g. the pump ring
  // while it's being handed between drivers), so nothing comes back different.
  try {
    const saved = JSON.parse(fs.readFileSync(statePath(), 'utf8'));
    for (const st of Object.values(saved)) {
      if (partial.effectId) st.effectId = partial.effectId;
      if (partial.params) st.params = { ...(st.params || {}), ...partial.params };
    }
    fs.writeFileSync(statePath(), JSON.stringify(saved, null, 2));
  } catch (_) {}
  syncKrakenRing(); // the ring follows "apply to all" like every other device
  // Remember the look so devices that appear later (e.g. the Kraken once
  // OpenRGB finds it) start with it too.
  const any = Object.values(deviceState)[0];
  if (any) {
    getSettings().lastLook = { effectId: partial.effectId ?? any.effectId, params: { ...(any.params || {}) } };
    try { fs.writeFileSync(settingsPath(), JSON.stringify(appSettings, null, 2)); } catch (_) {}
  }
  return { ok: true };
});

// ---------- Profiles ----------
// Profiles are stored by STABLE device identity (deviceKey), not by OpenRGB's
// device index. Index order changes between launches / PCs, so the old format
// could apply one device's settings to another. Applying a profile now also
// persists it, so it survives a restart.

function readProfiles() {
  try { return JSON.parse(fs.readFileSync(profilesPath(), 'utf8')); } catch (_) { return {}; }
}

ipcMain.handle('profiles:load', () => readProfiles());

ipcMain.handle('profiles:save', (_e, name) => {
  const all = readProfiles();
  const byKey = {};
  for (const d of devices) {
    if (deviceState[d.id]) byKey[deviceKey(d)] = JSON.parse(JSON.stringify(deviceState[d.id]));
  }
  all[name] = { version: 2, byKey };
  try {
    fs.writeFileSync(profilesPath(), JSON.stringify(all, null, 2));
  } catch (e) {
    return { ok: false, error: e.message };
  }
  return { ok: true, profiles: Object.keys(all) };
});

ipcMain.handle('profiles:apply', (_e, name) => {
  try {
    const p = readProfiles()[name];
    if (!p) return { ok: false, error: 'Profile not found' };
    if (p.version === 2 && p.byKey) {
      for (const d of devices) {
        const s = p.byKey[deviceKey(d)];
        if (s) deviceState[d.id] = JSON.parse(JSON.stringify(s));
      }
    } else {
      deviceState = p; // legacy profile (saved by device index)
    }
    lastSent = {};      // push the new look immediately
    saveDeviceStates(); // and keep it across restarts
    syncKrakenRing();   // profiles cover the ring too
    return { ok: true, state: deviceState };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

// ---------- Kraken LCD ----------

function lcdHandler(fn) {
  return async (_e, ...args) => {
    if (!krakenAllowed()) {
      return { ok: false, error: hw.krakenBootloader
        ? 'Kraken is stuck in bootloader mode: shut down, switch the PSU off for 30 s, then boot.'
        : 'No NZXT Kraken detected on this PC.' };
    }
    try {
      return { ok: true, result: await fn(...args) };
    } catch (e) {
      return { ok: false, error: e.message };
    }
  };
}

ipcMain.handle('lcd:init', lcdHandler(lcd.initialize));
ipcMain.handle('lcd:status', lcdHandler(lcd.status));
ipcMain.handle('lcd:liquid', lcdHandler(lcd.setLiquidMode));
ipcMain.handle('lcd:brightness', lcdHandler(lcd.setBrightness));
ipcMain.handle('lcd:orientation', lcdHandler(lcd.setOrientation));
ipcMain.handle('lcd:gif', lcdHandler(lcd.setGif));
ipcMain.handle('lcd:image', lcdHandler(lcd.setImage));
ipcMain.handle('lcd:pushFrame', lcdHandler(lcd.pushFrame));
ipcMain.handle('lcd:ring', lcdHandler(lcd.setRing));
ipcMain.handle('lcd:info', lcdHandler(lcd.getLcdInfo));

// ---------- GIPHY: download a picked GIF locally, then play it natively ----------
ipcMain.handle('giphy:download', async (_e, url, id) => {
  try {
    if (!/^https:\/\/([a-z0-9-]+\.)*giphy\.com\//i.test(url)) {
      return { ok: false, error: 'Only giphy.com URLs are allowed' };
    }
    const dir = path.join(app.getPath('userData'), 'giphy');
    fs.mkdirSync(dir, { recursive: true });
    const dest = path.join(dir, String(id).replace(/[^a-z0-9_-]/gi, '') + '.gif');
    if (!fs.existsSync(dest)) {
      const res = await fetch(url);
      if (!res.ok) return { ok: false, error: 'Download failed: HTTP ' + res.status };
      fs.writeFileSync(dest, Buffer.from(await res.arrayBuffer()));
    }
    return { ok: true, path: dest };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('lcd:pickImage', async () => {
  if (!krakenAllowed()) return { ok: false, error: 'No NZXT Kraken detected on this PC.' };
  const r = await dialog.showOpenDialog(win, {
    title: 'Choose LCD image',
    filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'gif', 'bmp'] }],
    properties: ['openFile'],
  });
  if (r.canceled || !r.filePaths.length) return { ok: false, error: 'canceled' };
  try {
    const p = r.filePaths[0];
    // No silent fallback: if the GIF path fails we want to SEE the error,
    // not quietly get the first frame as a static image.
    const result = p.toLowerCase().endsWith('.gif')
      ? await lcd.setGif(p)
      : await lcd.setImage(p);
    return { ok: true, path: p, result };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

app.whenReady().then(() => {
  createWindow();
  createTray();
  syncTask();
  startSensors();
  registerActions();
  // Special hardware (Kraken, MonsGeek HE) only gets its driver on PCs that
  // actually have it. Re-checked every 3 minutes so a newly plugged device is
  // picked up without a restart.
  const refreshAndNotify = async () => {
    if (!connected) return;
    try {
      const devs = await refreshDevices();
      if (win && !win.isDestroyed()) win.webContents.send('devices-updated', { devices: devs, state: deviceState });
    } catch (_) {}
  };
  const applyHw = (h, prev) => {
    // MonsGeek keyboard driver: when it finds/loses the keyboard, refresh the
    // device list so its card appears/disappears on the Devices page.
    if (h.monsgeek !== false) monsgeek.start(refreshAndNotify);
    const changed = !prev || prev.kraken !== h.kraken || prev.monsgeek !== h.monsgeek;
    if (changed && prev) {
      if (win && !win.isDestroyed()) win.webContents.send('hw-changed', h);
      refreshAndNotify();
      if (h.kraken) syncKrakenRing(true);
    }
  };
  hwReady.then((h) => applyHw(h, null));
  setInterval(async () => {
    const prev = { ...hw };
    try { hw = await hwdetect.detect(); } catch (_) { return; }
    applyHw(hw, prev);
  }, 180000);
  // GPUs revert to onboard RGB after sleep/unlock — re-assert Direct mode.
  powerMonitor.on('resume', reassertAfterResume);
  powerMonitor.on('unlock-screen', reassertAfterResume);
});
// We hide to the tray instead of closing, so only quit when the user asked to.
app.on('window-all-closed', () => { if (app.isQuitting) app.quit(); });
app.on('will-quit', () => {
  if (saveTimer) { clearTimeout(saveTimer); try { saveDeviceStates(); } catch (_) {} } // flush pending save
  if (openrgbProc) { try { openrgbProc.kill(); } catch (_) {} }
  if (sensorsProc) { try { sensorsProc.kill(); } catch (_) {} }
  try { lcd.shutdown(); } catch (_) {} // release the Kraken USB for next launch
  try { peripherals.shutdown(); } catch (_) {}
  try { globalShortcut.unregisterAll(); } catch (_) {}
  // Hand the keyboard back its own static color so it isn't frozen in sync mode.
  try { monsgeek.shutdown(monsgeekLastHex); } catch (_) {}
});
