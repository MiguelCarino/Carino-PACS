/* ============================================================
   Carino PACS desktop — Electron tray agent.
   ------------------------------------------------------------
   Runs the Python DICOM engine (`pacs serve --receive --watch`) as a
   background child process and shows a tray icon. Clicking the tray
   opens the dashboard in a native window; closing the window hides it
   back to the tray (the engine keeps running). Quit (from the tray)
   stops the engine and exits.

   Dev run:   cd desktop && npm install && npm start
   ============================================================ */
"use strict";

const { app, BrowserWindow, Tray, Menu, dialog, nativeImage, shell } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");
const os = require("os");

const ROOT = path.join(__dirname, "..");            // the Carino PACS repo root
const ASSETS = path.join(__dirname, "assets");
const DATA_DIR = path.join(os.homedir(), "CarinoPACS");   // config + data + logs live here

let tray = null;
let win = null;
let py = null;
let serverUrl = "http://127.0.0.1:8042/";

// ---- helpers ------------------------------------------------------------
// Config + data (received/outgoing/sent) live in a writable dir: the OS
// per-user data dir in a packaged app, the repo root in dev.
function configPath() {
  // Config + data + logs live in ~/CarinoPACS for both dev and packaged builds.
  return path.join(DATA_DIR, "config.json");
}

function webConfig() {
  let host = "127.0.0.1";
  let port = 8042;
  try {
    const cfg = JSON.parse(fs.readFileSync(configPath(), "utf8"));
    if (cfg.web && cfg.web.port) port = cfg.web.port;
    if (cfg.web && cfg.web.host && cfg.web.host !== "0.0.0.0") host = cfg.web.host;
  } catch (e) { /* first run / no config yet → defaults */ }
  return { host, port };
}

// The command that runs the DICOM engine: a bundled PyInstaller binary in a
// packaged app, or `python -m pacs` from the project .venv during development.
function engineCommand(host, port) {
  const isWin = process.platform === "win32";
  // NB: --config is a global flag, so it must precede the `serve` subcommand.
  const common = ["--config", configPath(), "serve", "--host", host, "--port", String(port),
    "--receive", "--watch"];
  if (app.isPackaged) {
    const bin = path.join(process.resourcesPath, "engine", "pacs-engine",
      isWin ? "pacs-engine.exe" : "pacs-engine");
    return { cmd: bin, args: common, cwd: DATA_DIR };
  }
  const venv = isWin
    ? path.join(ROOT, ".venv", "Scripts", "python.exe")
    : path.join(ROOT, ".venv", "bin", "python");
  const py = fs.existsSync(venv) ? venv : (isWin ? "python" : "python3");
  return { cmd: py, args: ["-m", "pacs", ...common], cwd: DATA_DIR };
}

function startEngine() {
  const { host, port } = webConfig();
  serverUrl = `http://${host === "0.0.0.0" ? "127.0.0.1" : host}:${port}/`;
  const { cmd, args, cwd } = engineCommand(host, port);
  try { fs.mkdirSync(cwd, { recursive: true }); } catch (e) { /* ignore */ }
  py = spawn(cmd, args, { cwd, env: process.env, windowsHide: true });
  py.stdout.on("data", (d) => process.stdout.write(`[pacs] ${d}`));
  py.stderr.on("data", (d) => process.stderr.write(`[pacs] ${d}`));
  py.on("exit", (code) => {
    py = null;
    if (!app.isQuitting) {
      dialog.showErrorBox("Carino PACS engine stopped",
        `The DICOM engine exited (code ${code}).` +
        (app.isPackaged ? "" : "\n\nIn dev, run ./setup.sh (or setup.ps1) so the .venv exists."));
    }
  });
  py.on("error", (err) => {
    dialog.showErrorBox("Could not start Carino PACS engine",
      `Failed to launch the engine:\n${cmd}\n\n${err.message}` +
      (app.isPackaged ? "" : "\n\nRun ./setup.sh (or setup.ps1) first."));
  });
}

function waitForServer(timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    const probe = () => {
      const req = http.get(serverUrl + "api/status", (res) => {
        res.resume();
        if (res.statusCode === 200) return resolve(true);
        retry();
      });
      req.on("error", retry);
      req.setTimeout(1500, () => req.destroy());
    };
    const retry = () => (Date.now() > deadline ? resolve(false) : setTimeout(probe, 400));
    probe();
  });
}

// ---- window + tray ------------------------------------------------------
function createWindow() {
  win = new BrowserWindow({
    width: 1150,
    height: 820,
    show: false,
    title: "Carino PACS",
    icon: path.join(ASSETS, "icon.png"),
    backgroundColor: "#050505",
    autoHideMenuBar: true,       // no menu bar (Alt won't reveal it either)
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  win.loadURL(serverUrl);
  // External links open in the real browser, not inside the app window.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  // Closing hides to tray instead of quitting the agent.
  win.on("close", (e) => {
    if (!app.isQuitting) { e.preventDefault(); win.hide(); }
  });
}

function showWindow() {
  if (!win) createWindow();
  win.show();
  win.focus();
}

function buildMenu() {
  return Menu.buildFromTemplate([
    { label: "Open Carino PACS", click: showWindow },
    { type: "separator" },
    {
      label: "Start at login",
      type: "checkbox",
      checked: app.getLoginItemSettings().openAtLogin,
      click: (item) => app.setLoginItemSettings({ openAtLogin: item.checked }),
    },
    { type: "separator" },
    { label: "Quit Carino PACS", click: quitApp },
  ]);
}

function createTray() {
  const img = nativeImage.createFromPath(path.join(ASSETS, "tray.png"));
  tray = new Tray(img);
  tray.setToolTip("Carino PACS — DICOM store (running)");
  tray.setContextMenu(buildMenu());
  tray.on("click", showWindow);          // left-click opens the window (Win/Linux)
  tray.on("double-click", showWindow);
}

function quitApp() {
  app.isQuitting = true;
  if (py) { try { py.kill(); } catch (e) { /* ignore */ } py = null; }
  app.quit();
}

// ---- lifecycle ----------------------------------------------------------
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showWindow);   // re-launch focuses the existing agent

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);   // remove the default app/menu bar entirely
    startEngine();
    createTray();
    const up = await waitForServer();
    if (!up) {
      dialog.showErrorBox("Carino PACS", "The dashboard did not come up in time. It may still be starting — click the tray icon to retry.");
    }
    createWindow();
    if (up) showWindow();
  });

  // Do NOT quit when the window is closed — this is a tray agent.
  app.on("window-all-closed", (e) => { /* keep running in the tray */ });
  app.on("activate", showWindow);           // macOS dock click
  app.on("before-quit", () => { app.isQuitting = true; if (py) { try { py.kill(); } catch (e) {} } });
}
