// Entry point. Decides whether THIS process runs the app (see launcher.js):
//   not elevated  -> start the elevated copy (task or one UAC prompt), exit
//   already open  -> the running copy shows its window, exit
//   otherwise     -> run the app
const { app } = require('electron');
const launcher = require('./launcher');

if (launcher.handOff()) {
  app.exit(0);
} else if (!app.requestSingleInstanceLock()) {
  app.exit(0);
} else {
  require('./main');
}
