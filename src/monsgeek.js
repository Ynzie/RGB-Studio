// MonsGeek M1 V5 HE keyboard via our own driver (kraken/monsgeek_he.py).
//
// Stock firmware, no flashing: the driver puts the keyboard in its built-in
// "Screen Sync" LED mode and we stream one live color on the shared effects
// clock, so the keyboard stays in sync with every other device.

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

let proc = null;
let available = false;
let deviceName = 'MonsGeek M1 V5 HE';
let lastError = '';
let buf = '';
let onChange = () => {};

function scriptPath() {
  const candidates = [];
  if (process.env.PORTABLE_EXECUTABLE_DIR) {
    candidates.push(path.join(process.env.PORTABLE_EXECUTABLE_DIR, '..', 'kraken', 'monsgeek_he.py'));
  }
  candidates.push(path.join(__dirname, '..', 'kraken', 'monsgeek_he.py'));
  if (process.resourcesPath) {
    candidates.push(path.join(process.resourcesPath, '..', '..', '..', 'kraken', 'monsgeek_he.py'));
    candidates.push(path.join(process.resourcesPath, 'kraken', 'monsgeek_he.py'));
  }
  return candidates.find((p) => { try { return fs.existsSync(p); } catch (_) { return false; } });
}

// Settings requests (Peripherals page) are answered with the same rid.
let nextRid = 1;
const waiting = new Map();

function handleLine(line) {
  let msg;
  try { msg = JSON.parse(line); } catch (_) { return; }
  if (msg.rid != null) {
    const w = waiting.get(msg.rid);
    if (w) { waiting.delete(msg.rid); clearTimeout(w.t); w.resolve(msg); }
    return;
  }
  if (msg.event === 'ready') {
    const was = available;
    available = true;
    lastError = '';
    if (msg.name) deviceName = 'MonsGeek ' + msg.name;
    if (!was) onChange(true);
  } else if (msg.event === 'error') {
    lastError = msg.error || 'unknown error';
    const was = available;
    available = false;
    if (was) onChange(false);
  }
}

function start(changeCb) {
  if (changeCb) onChange = changeCb;
  if (proc) return;
  const script = scriptPath();
  if (!script) { lastError = 'monsgeek_he.py not found'; return; }
  try {
    // A Python that actually has hidapi (plain `py` may be a newer Python
    // without it — that's what silently killed the keyboard).
    const py = require('./python').pythonFor('hid');
    proc = spawn(py.cmd, [...py.args, script, 'serve'],
      { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true });
  } catch (e) { lastError = e.message; proc = null; return; }
  proc.stdout.on('data', (c) => {
    buf += c.toString();
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) handleLine(line);
    }
  });
  const dead = () => {
    proc = null;
    for (const [rid, w] of waiting) { clearTimeout(w.t); w.resolve({ ok: false, error: 'keyboard driver stopped' }); waiting.delete(rid); }
    if (available) { available = false; onChange(false); }
  };
  proc.on('error', dead);
  proc.on('exit', dead);
}

function send(obj) {
  if (!proc || !proc.stdin.writable) return;
  try { proc.stdin.write(JSON.stringify(obj) + '\n'); } catch (_) {}
}

// Keyboard settings: kb_scan / kb_save / kb_restore, answered by rid.
function request(cmd, extra, timeoutMs = 60000) {
  return new Promise((resolve) => {
    if (!proc || !proc.stdin.writable) return resolve({ ok: false, error: 'MonsGeek keyboard not found (plug it in with the cable).' });
    const rid = nextRid++;
    const t = setTimeout(() => { waiting.delete(rid); resolve({ ok: false, error: 'The keyboard took too long to answer.' }); }, timeoutMs);
    waiting.set(rid, { resolve, t });
    send({ cmd, rid, ...extra });
  });
}

// Fire-and-forget: the caller throttles (~30 fps) and dedupes.
function streamColor(hex) { send({ cmd: 'color', hex }); }

// Hand lighting back to the keyboard (static color, stored on-board) on quit,
// so it doesn't sit frozen in sync mode while RGB Studio isn't running.
function shutdown(lastHex) {
  if (!proc) return;
  if (available && lastHex) send({ cmd: 'restore', hex: lastHex });
  send({ cmd: 'quit' });
  const p = proc;
  setTimeout(() => { try { p.kill(); } catch (_) {} }, 400);
  proc = null;
}

module.exports = {
  start,
  streamColor,
  shutdown,
  request,
  isAvailable: () => available,
  name: () => deviceName,
  lastError: () => lastError,
};
