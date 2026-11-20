// Kraken Elite V2 LCD/ring/status via our own USB driver (kraken/nzxt_kraken.py).
//
// liquidctl can't drive the 2024 Elite screen, so we ported the protocol to a
// Python script and talk to it here. The script runs once in "serve" mode and
// we stream one JSON command per line over stdin, keeping the USB link open so
// frames (GIF playback, sensor overlay) don't pay Python-startup cost each time.

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

let proc = null;
let currentScript = null;  // resolved driver path, shown in init/errors for debugging
let stdoutBuf = '';
const pending = [];        // FIFO of { resolve, reject } awaiting a response line

function scriptPath() {
  const candidates = [];
  // The portable exe lives in <root>\dist, so <root>\kraken is the LIVE source
  // driver. Prefer it so edits to the driver take effect on relaunch without a
  // full rebuild (the bundled copy is only a fallback).
  if (process.env.PORTABLE_EXECUTABLE_DIR) {
    candidates.push(path.join(process.env.PORTABLE_EXECUTABLE_DIR, '..', 'kraken', 'nzxt_kraken.py'));
  }
  candidates.push(path.join(__dirname, '..', 'kraken', 'nzxt_kraken.py'));             // npm start / dev
  if (process.resourcesPath) {
    // dist\win-unpacked\resources -> ..\..\.. = project root (live driver when
    // launching win-unpacked\RGB Studio.exe directly)
    candidates.push(path.join(process.resourcesPath, '..', '..', '..', 'kraken', 'nzxt_kraken.py'));
    candidates.push(path.join(process.resourcesPath, 'kraken', 'nzxt_kraken.py'));     // bundled fallback
  }
  return candidates.find((p) => {
    try { return fs.existsSync(p); } catch (_) { return false; }
  });
}

// Use a Python that actually has the driver's packages (plain `py` may point
// at a newer Python without them — that's what broke the LCD).
const { pythonFor } = require('./python');
let lastPlainLine = ''; // non-JSON stdout (e.g. "missing pyusb") for error messages

function rejectAll(err) {
  while (pending.length) pending.shift().reject(err);
}

function ensureProc() {
  if (proc) return;
  const script = scriptPath();
  if (!script) throw new Error('Kraken driver not found (kraken/nzxt_kraken.py missing).');
  currentScript = script;
  const py = pythonFor('usb, libusb_package, PIL');
  lastPlainLine = '';
  proc = spawn(py.cmd, [...py.args, script, 'serve'], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  proc.stdout.on('data', onStdout);
  proc.stderr.on('data', () => {}); // driver debug goes to stderr; ignore
  proc.on('error', (e) => { rejectAll(new Error('Kraken driver failed to start: ' + e.message)); proc = null; });
  proc.on('exit', () => {
    rejectAll(new Error('Kraken driver process exited' + (lastPlainLine ? ': ' + lastPlainLine : '')));
    proc = null;
  });
}

function onStdout(chunk) {
  stdoutBuf += chunk.toString();
  let nl;
  while ((nl = stdoutBuf.indexOf('\n')) >= 0) {
    const line = stdoutBuf.slice(0, nl).trim();
    stdoutBuf = stdoutBuf.slice(nl + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch (_) { lastPlainLine = line.slice(0, 200); continue; }
    if (msg.event === 'ready') continue;          // startup banner, not a reply
    const p = pending.shift();
    if (!p) continue;
    if (msg.ok === false) p.reject(new Error(msg.error || 'Kraken driver error'));
    else p.resolve(msg);
  }
}

function send(obj) {
  return new Promise((resolve, reject) => {
    try { ensureProc(); } catch (e) { return reject(e); }
    pending.push({ resolve, reject });
    proc.stdin.write(JSON.stringify(obj) + '\n');
  });
}

// ---------- API (same names the IPC layer already expects) ----------

async function initialize() {
  await send({ cmd: 'status' }); // starts the process + confirms the link
  return `Kraken driver connected (${currentScript}).`;
}

async function status() {
  const r = await send({ cmd: 'status' });
  const st = r.status;
  if (!st) return { raw: 'No status yet from Kraken (try again).' };
  // Shape it like the old liquidctl output so the renderer keeps working.
  return {
    raw: `Liquid temperature    ${st.liquid} °C\nPump speed            ${st.pump} rpm`,
    'Liquid temperature': { value: st.liquid, unit: '°C' },
    'Pump speed': { value: st.pump, unit: 'rpm' },
  };
}

async function setImage(imagePath) {
  await send({ cmd: 'image', path: imagePath });
  return 'Image sent.';
}

async function setGif(gifPath) {
  await send({ cmd: 'gif', path: gifPath });
  return 'GIF sent.';
}

// Sensor overlay + JS GIF playback push a rendered PNG data URL each frame.
async function pushFrame(dataUrl) {
  const b64 = dataUrl.replace(/^data:image\/png;base64,/, '');
  await send({ cmd: 'frame', png: b64 });
  return 'ok';
}

// Ring lighting through the same driver (mode + hex colors + speed 0-4).
// `colors` may be a single "RRGGBB" string or an array of them.
async function setRing(mode, colors, speed) {
  const list = (Array.isArray(colors) ? colors : (colors ? [colors] : []))
    .map((c) => String(c).replace('#', ''));
  await send({ cmd: 'ring', mode, colors: list, speed });
  return 'ok';
}

// Streaming variant: no USB reopen, bare write after the first handshake.
// Used by the effects engine to keep the ring on the shared software clock.
async function setRingDirect(mode, colors, speed) {
  const list = (Array.isArray(colors) ? colors : (colors ? [colors] : []))
    .map((c) => String(c).replace('#', ''));
  await send({ cmd: 'ring_direct', mode, colors: list, speed });
  return 'ok';
}

// Per-LED frame from the effects engine: ring + (optional) Kraken fan port,
// each an array of "RRGGBB". Keeps the ring on the app's shared clock.
async function setLeds(ring, fans, reinit) {
  await send({ cmd: 'leds', ring: ring || [], fans: fans || [], reinit: !!reinit });
  return 'ok';
}

// { ring: <LEDs>, fans: <LEDs on the Kraken's RGB fan port> }
async function lightInfo() {
  const r = await send({ cmd: 'lightinfo' });
  return r.info;
}

async function setLiquidMode() {
  return 'Native liquid-temp mode is a liquidctl feature — use the sensor overlay instead.';
}

// LCD panel settings (now real driver commands, not no-ops).
async function setBrightness(value) {
  await send({ cmd: 'brightness', value: +value });
  return 'ok';
}
async function setOrientation(value) {
  await send({ cmd: 'orientation', value: +value });
  return 'ok';
}
async function getLcdInfo() {
  const r = await send({ cmd: 'lcdinfo' });
  return r.info;
}

// Kill the driver on app exit — it holds the Kraken USB exclusively, so a
// leftover process would lock out the next launch (the "access denied" trap).
function shutdown() {
  if (!proc) return;
  try { proc.stdin.write(JSON.stringify({ cmd: 'quit' }) + '\n'); } catch (_) {}
  try { proc.kill(); } catch (_) {}
  proc = null;
}

module.exports = {
  initialize,
  status,
  setImage,
  setGif,
  setLiquidMode,
  setBrightness,
  setOrientation,
  getLcdInfo,
  pushFrame,
  setRing,
  setRingDirect,
  setLeds,
  lightInfo,
  shutdown,
};
