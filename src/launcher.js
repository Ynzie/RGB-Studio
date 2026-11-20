// One exe, no helper scripts.
//
// RGB Studio must run elevated (RAM / motherboard lighting needs admin). A
// Windows scheduled task set to "run with highest privileges" can start it
// elevated WITHOUT a UAC prompt, so:
//   - Opened normally (not elevated) and our task points at THIS exe:
//       run the task -> the elevated copy starts, no prompt.
//   - No task yet / it points somewhere else (first run, moved, other PC):
//       ask for admin ONCE (UAC); the elevated copy then registers the task.
//   - The elevated copy keeps the task pointed at itself. The task's logon
//       trigger is the "Start with Windows" tray option.
// Replaces "Launch RGB Studio.vbs", enable-startup.ps1 and Setup Auto-Start.bat.

const { app } = require('electron');
const path = require('path');
const fs = require('fs');
const { execSync, execFile, spawn } = require('child_process');

const TASK = 'RGB Studio';
const psQuote = (s) => "'" + String(s).replace(/'/g, "''") + "'";
// -EncodedCommand sidesteps every command-line quoting problem.
const psArgs = (script) => ['-NoProfile', '-NonInteractive', '-EncodedCommand',
  Buffer.from(script, 'utf16le').toString('base64')];

function isElevated() {
  if (process.platform !== 'win32') return true;
  try {
    execSync('net session', { stdio: 'ignore', windowsHide: true });
    return true;
  } catch (_) {
    return false;
  }
}

function taskExeSync() {
  try {
    const out = execSync(
      'powershell ' + psArgs(`(Get-ScheduledTask -TaskName '${TASK}' -ErrorAction Stop).Actions[0].Execute`).join(' '),
      { windowsHide: true, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 20000 }
    );
    return out.trim().replace(/^"|"$/g, '') || null;
  } catch (_) {
    return null;
  }
}

const samePath = (a, b) => path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase();
const showFlag = () => path.join(app.getPath('userData'), 'show-window.flag');

// Called before anything else. Returns true if this (non-elevated) process
// handed off to an elevated copy and must exit now.
function handOff() {
  if (process.platform !== 'win32' || !app.isPackaged || isElevated()) return false;
  const exe = process.execPath;
  // The task starts the app hidden in the tray; this flag tells that copy the
  // user actually clicked the app, so it should open its window.
  try {
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    fs.writeFileSync(showFlag(), String(Date.now()));
  } catch (_) {}
  const t = taskExeSync();
  if (t && samePath(t, exe)) {
    try {
      execSync(`schtasks /run /tn "${TASK}"`, { stdio: 'ignore', windowsHide: true });
      return true;
    } catch (_) { /* task broken/disabled: fall through to UAC */ }
  }
  spawn('powershell', psArgs(`Start-Process -FilePath ${psQuote(exe)} -Verb RunAs`),
    { detached: true, stdio: 'ignore', windowsHide: true }).unref();
  return true;
}

// True (once) if a hand-off in the last 2 minutes asked for the window.
function consumeShowRequest() {
  try {
    const f = showFlag();
    if (!fs.existsSync(f)) return false;
    const age = Date.now() - (+fs.readFileSync(f, 'utf8') || 0);
    fs.unlinkSync(f);
    return age < 120000;
  } catch (_) {
    return false;
  }
}

// Elevated copy only: make the task launch THIS exe (prompt-free launches),
// with a logon trigger only when "Start with Windows" is on.
function syncLogonTask(startWithWindows, cb) {
  const done = (err, out) => cb && cb(err, out);
  if (process.platform !== 'win32' || !app.isPackaged) return done(null, 'skipped (dev)');
  if (!isElevated()) return done(new Error('not elevated'), '');
  const want = startWithWindows ? '$true' : '$false';
  const script = [
    `$exe = ${psQuote(process.execPath)}`,
    `$want = ${want}`,
    `$t = Get-ScheduledTask -TaskName '${TASK}' -ErrorAction SilentlyContinue`,
    `if ($t -and $t.Actions[0].Execute -eq $exe -and $t.Settings.MultipleInstances -eq 'Parallel' -and $t.Settings.Priority -eq 4 -and ((($t.Triggers | Measure-Object).Count -gt 0) -eq $want)) { 'unchanged'; exit }`,
    `$u = [Security.Principal.WindowsIdentity]::GetCurrent().Name`,
    `$a = New-ScheduledTaskAction -Execute $exe -Argument '--minimized' -WorkingDirectory (Split-Path $exe)`,
    `$p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Highest`,
    // Parallel: "open the app" must still work while the tray copy runs (the
    // new copy just tells the running one to show its window and exits).
    // Priority 4 = normal; tasks default to below-normal, which can stutter effects.
    `$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances Parallel -Priority 4`,
    `$args2 = @{ TaskName = '${TASK}'; Action = $a; Principal = $p; Settings = $s; Force = $true }`,
    `if ($want) { $args2.Trigger = New-ScheduledTaskTrigger -AtLogOn -User $u }`,
    `Register-ScheduledTask @args2 | Out-Null`,
    `if ($want) { 'registered (start with Windows)' } else { 'registered (no auto-start)' }`,
  ].join('\n');
  execFile('powershell', psArgs(script), { windowsHide: true, timeout: 30000 },
    (err, out) => done(err, String(out || '').trim()));
}

module.exports = { handOff, consumeShowRequest, syncLogonTask, isElevated };
