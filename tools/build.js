// Builds RGB Studio.
//   npm run build      -> dist\win-unpacked\RGB Studio.exe   (about a minute)
//   npm run installer  -> dist\RGB-Studio-Setup.exe          (for other PCs)
//
// Putting the icon on the exe uses electron-builder's rcedit download. If
// that step fails on this PC, build again without it rather than failing -
// the window and tray still show the icon.
const path = require('path');
const builder = require('electron-builder');
const pkg = require('../package.json');

const projectDir = path.join(__dirname, '..');
const target = process.argv.includes('--installer') ? 'nsis' : 'dir';

function run(editExe) {
  // Only the override: electron-builder merges this into package.json's
  // "build" section (passing the whole section doubled the resource copies).
  const config = { win: { ...pkg.build.win, signAndEditExecutable: editExe } };
  return builder.build({
    projectDir,
    targets: builder.Platform.WINDOWS.createTarget(target),
    config,
  });
}

(async () => {
  const t0 = Date.now();
  try {
    await run(true);
  } catch (e) {
    console.error('\nBuild with the exe icon failed: ' + (e && e.message));
    console.error('Retrying without editing the exe...\n');
    await run(false);
  }
  console.log(`\nBuilt in ${Math.round((Date.now() - t0) / 1000)} s`);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
