// Picks a Python interpreter that actually has a driver's libraries installed.
//
// Plain `py` follows whatever Python was installed last (a new 3.15 silently
// took over and broke both drivers, whose packages live in 3.12). Instead we
// probe the installed versions and use the first one where the driver's
// imports succeed. Results are cached per module list.

const { spawnSync } = require('child_process');

const cache = {};

function candidates() {
  if (process.platform !== 'win32') return [['python3', []]];
  return [
    ['py', ['-3.12']],
    ['py', ['-3.13']],
    ['py', ['-3.11']],
    ['py', ['-3.14']],
    ['py', []],
    ['python', []],
  ];
}

// imports: e.g. 'usb, libusb_package, PIL'  ->  { cmd, args } or a best guess
function pythonFor(imports) {
  if (cache[imports]) return cache[imports];
  for (const [cmd, args] of candidates()) {
    try {
      const r = spawnSync(cmd, [...args, '-c', `import ${imports}`], {
        windowsHide: true,
        timeout: 15000,
        stdio: 'ignore',
      });
      if (r.status === 0) {
        cache[imports] = { cmd, args };
        return cache[imports];
      }
    } catch (_) { /* try next */ }
  }
  // Nothing has the packages: fall back to the default so the driver can
  // report the missing-package error itself.
  const fallback = { cmd: process.platform === 'win32' ? 'py' : 'python3', args: [] };
  cache[imports] = fallback;
  return fallback;
}

module.exports = { pythonFor };
