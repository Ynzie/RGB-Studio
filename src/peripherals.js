// Mouse / keyboard settings stored in the devices' own memory (Peripherals page).
// Talks to peripherals/logi_hidpp.py in "serve" mode (one JSON line per command),
// started only when the Peripherals page asks for it.

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const { pythonFor } = require('./python');

let proc = null;
let buf = '';
const pending = [];

function scriptPath() {
  const c = [path.join(__dirname, '..', 'peripherals', 'logi_hidpp.py')]; // npm start
  if (process.resourcesPath) {
    c.push(path.join(process.resourcesPath, '..', '..', '..', 'peripherals', 'logi_hidpp.py')); // live copy
    c.push(path.join(process.resourcesPath, 'peripherals', 'logi_hidpp.py'));                   // bundled
  }
  return c.find((p) => { try { return fs.existsSync(p); } catch (_) { return false; } });
}

function ensure() {
  if (proc) return;
  const script = scriptPath();
  if (!script) throw new Error('Peripheral driver missing (peripherals/logi_hidpp.py).');
  const py = pythonFor('usb, libusb_package, PIL');
  proc = spawn(py.cmd, [...py.args, script, 'serve'], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  proc.stdout.on('data', (chunk) => {
    buf += chunk.toString();
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      let msg;
      try { msg = JSON.parse(line); } catch (_) { continue; }
      if (msg.event === 'ready') continue;
      const p = pending.shift();
      if (!p) continue;
      if (msg.ok === false) p.reject(new Error(msg.error || 'driver error'));
      else p.resolve(msg);
    }
  });
  proc.stderr.on('data', () => {});
  const fail = (e) => { while (pending.length) pending.shift().reject(e); proc = null; };
  proc.on('error', (e) => fail(new Error('Peripheral driver failed to start: ' + e.message)));
  proc.on('exit', () => fail(new Error('Peripheral driver stopped')));
}

function send(obj, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    try { ensure(); } catch (e) { return reject(e); }
    const entry = { resolve, reject };
    pending.push(entry);
    const t = setTimeout(() => {
      const i = pending.indexOf(entry);
      if (i >= 0) pending.splice(i, 1);
      reject(new Error('The mouse took too long to answer - move it to wake it up.'));
    }, timeoutMs);
    entry.resolve = (v) => { clearTimeout(t); resolve(v); };
    entry.reject = (e) => { clearTimeout(t); reject(e); };
    proc.stdin.write(JSON.stringify(obj) + '\n');
  });
}

module.exports = {
  scan: () => send({ cmd: 'scan' }),
  // cfg: { dpis, defaultIndex, shiftIndex, rateHz, buttons: [{kind, ...}] }
  saveMouse: (id, cfg, backupDir) => send({ ...cfg, cmd: 'save', id, backupDir }, 60000),
  restoreMouse: (id, backupDir) => send({ cmd: 'restore', id, backupDir }),
  shutdown() {
    if (!proc) return;
    try { proc.stdin.write('{"cmd":"quit"}\n'); } catch (_) {}
    try { proc.kill(); } catch (_) {}
    proc = null;
  },
};
