/* ============================================================
   Carino PACS desktop — Electron tray agent.
   ------------------------------------------------------------
   Runs the Python DICOM engine (`pacs serve --receive --watch`) as a
   background child process and shows a tray icon. The window shows a
   loading screen immediately, then the dashboard once the engine is up
   (or an error page pointing at the engine log). On first run it asks
   where to store data (default ~/CarinoPACS), creates the folders, and
   starts the service. Closing the window hides it to the tray; Quit (or
   the dashboard "Shut down service") stops the engine and exits.

   Dev run:   cd desktop && npm install && npm start
   ============================================================ */
"use strict";

const { app, BrowserWindow, Tray, Menu, dialog, nativeImage, shell } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");
const os = require("os");

const ROOT = path.join(__dirname, "..");
const ASSETS = path.join(__dirname, "assets");

let tray = null;
let win = null;
let py = null;
let dataDir = path.join(os.homedir(), "CarinoPACS");   // resolved at startup
let serverUrl = "http://127.0.0.1:8042/";

// ---- data folder / first run -------------------------------------------
function defaultDataDir() { return path.join(os.homedir(), "CarinoPACS"); }
function locationFile() { return path.join(app.getPath("userData"), "location.json"); }

function loadSavedDataDir() {
  try { const j = JSON.parse(fs.readFileSync(locationFile(), "utf8")); if (j && j.dir) return j.dir; } catch (e) {}
  return null;   // null → never configured (first run)
}
function saveDataDir(dir) {
  try {
    fs.mkdirSync(path.dirname(locationFile()), { recursive: true });
    fs.writeFileSync(locationFile(), JSON.stringify({ dir }));
  } catch (e) { /* non-fatal */ }
}
function ensureFolders(base) {
  ["", "received", "outgoing", "sent", "logs"].forEach((s) => {
    try { fs.mkdirSync(path.join(base, s), { recursive: true }); } catch (e) {}
  });
}

// First-run: show the default folder, let the user keep or change it, create it.
async function firstRunSetup() {
  const def = defaultDataDir();
  const r = await dialog.showMessageBox({
    type: "question",
    title: "Carino PACS — choose data folder",
    message: "Where should Carino PACS store its data?",
    detail: "Received images, the outgoing queue and logs are saved here:\n\n" + def +
            "\n\nUse this default, or choose another folder.",
    buttons: ["Use default", "Choose another…", "Quit"],
    defaultId: 0, cancelId: 2, noLink: true,
  });
  if (r.response === 2) return null;   // Quit
  let base = def;
  if (r.response === 1) {
    const pick = await dialog.showOpenDialog({
      title: "Choose the Carino PACS data folder",
      defaultPath: os.homedir(),
      properties: ["openDirectory", "createDirectory"],
      buttonLabel: "Use this folder",
    });
    if (!pick.canceled && pick.filePaths[0]) base = pick.filePaths[0];
  }
  ensureFolders(base);
  saveDataDir(base);
  return base;
}

// ---- engine ------------------------------------------------------------
function configPath() { return path.join(dataDir, "config.json"); }

function webConfig() {
  let host = "127.0.0.1", port = 8042;
  try {
    const cfg = JSON.parse(fs.readFileSync(configPath(), "utf8"));
    if (cfg.web && cfg.web.port) port = cfg.web.port;
    if (cfg.web && cfg.web.host && cfg.web.host !== "0.0.0.0") host = cfg.web.host;
  } catch (e) { /* first run / no config yet → defaults */ }
  return { host, port };
}

// A bundled PyInstaller binary in a packaged app, or `python -m pacs` in dev.
function engineCommand(host, port) {
  const isWin = process.platform === "win32";
  // NB: --config is a global flag, so it must precede the `serve` subcommand.
  const common = ["--config", configPath(), "serve", "--host", host, "--port", String(port), "--receive", "--watch"];
  if (app.isPackaged) {
    const bin = path.join(process.resourcesPath, "engine", "pacs-engine", isWin ? "pacs-engine.exe" : "pacs-engine");
    return { cmd: bin, args: common, cwd: dataDir };
  }
  const venv = isWin ? path.join(ROOT, ".venv", "Scripts", "python.exe") : path.join(ROOT, ".venv", "bin", "python");
  const runner = fs.existsSync(venv) ? venv : (isWin ? "python" : "python3");
  return { cmd: runner, args: ["-m", "pacs", ...common], cwd: dataDir };
}

function startEngine() {
  const { host, port } = webConfig();
  serverUrl = `http://${host === "0.0.0.0" ? "127.0.0.1" : host}:${port}/`;
  const { cmd, args, cwd } = engineCommand(host, port);
  try { fs.mkdirSync(cwd, { recursive: true }); } catch (e) {}

  // Mirror engine output to a file so packaged-build failures are diagnosable.
  let logStream = null;
  try { logStream = fs.createWriteStream(path.join(dataDir, "desktop-engine.log"), { flags: "a" }); } catch (e) {}
  const write = (d) => { const s = `[pacs] ${d}`; process.stdout.write(s); if (logStream) logStream.write(s); };
  if (logStream) logStream.write(`\n=== launch ${new Date().toISOString()} ===\n${cmd} ${args.join(" ")}\n`);

  try {
    // Force UTF-8 stdio so Windows (cp1252) doesn't crash on chars like → … —.
    const env = { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" };
    py = spawn(cmd, args, { cwd, env, windowsHide: true });
  } catch (err) {
    showError("Failed to launch the engine:\n" + cmd + "\n\n" + err.message);
    return;
  }
  py.stdout.on("data", write);
  py.stderr.on("data", write);
  py.on("exit", (code) => {
    py = null;
    if (app.isQuitting) return;
    if (code === 0) { app.isQuitting = true; app.quit(); return; }   // clean shutdown → quit
    showError("The DICOM engine stopped unexpectedly (exit code " + code + ").");
  });
  py.on("error", (err) => showError("Could not launch the engine:\n" + cmd + "\n\n" + err.message));
}

function waitForServer(timeoutMs = 40000) {
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

// ---- window + tray -----------------------------------------------------
function createWindow() {
  win = new BrowserWindow({
    width: 1150, height: 820, show: false,
    title: "Carino PACS", icon: path.join(ASSETS, "icon.png"),
    backgroundColor: "#050505", autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  win.loadFile(path.join(__dirname, "loading.html"));   // never a blank/black window
  win.webContents.setWindowOpenHandler(({ url }) => {
    // The bundled DICOM-editor opens in its OWN Electron window (not the system
    // browser). action:"allow" keeps window.opener wired, so the PACS→editor
    // postMessage bridge still delivers the study. Everything else (GitHub,
    // LinkedIn, …) opens in the user's browser.
    try {
      const u = new URL(url);
      const local = u.hostname === "127.0.0.1" || u.hostname === "localhost";
      if (local && u.pathname.startsWith("/editor")) {
        return {
          action: "allow",
          overrideBrowserWindowOptions: {
            width: 1200, height: 860,
            title: "DICOM Editor — Carino", icon: path.join(ASSETS, "icon.png"),
            backgroundColor: "#000000", autoHideMenuBar: true,
            webPreferences: { contextIsolation: true, nodeIntegration: false },
          },
        };
      }
    } catch (_) { /* not a parseable URL — fall through to external */ }
    shell.openExternal(url);
    return { action: "deny" };
  });
  win.on("close", (e) => { if (!app.isQuitting) { e.preventDefault(); win.hide(); } });
}

function showError(msg) {
  const logPath = path.join(dataDir, "desktop-engine.log");
  const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const html = "<!doctype html><meta charset=utf-8><style>" +
    "body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#050505;" +
    "color:#f5f5f5;font-family:system-ui,-apple-system,sans-serif;text-align:center;padding:24px}" +
    ".b{max-width:560px}h2{color:#ef4444;margin:0 0 12px}p{color:#8a8a8a;line-height:1.55}" +
    "code{color:#f5f5f5;background:#111;padding:2px 6px;border-radius:4px;font-size:.85em;word-break:break-all}</style>" +
    "<div class=b><h2>Carino PACS couldn't start</h2><p>" + esc(msg).replace(/\n/g, "<br>") + "</p>" +
    "<p>Details were written to:<br><code>" + esc(logPath) + "</code></p></div>";
  if (win && !win.isDestroyed()) {
    win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
    win.show();
  }
}

function showWindow() { if (!win) createWindow(); win.show(); win.focus(); }

function buildMenu() {
  return Menu.buildFromTemplate([
    { label: "Open Carino PACS", click: showWindow },
    { type: "separator" },
    {
      label: "Start at login", type: "checkbox",
      checked: app.getLoginItemSettings().openAtLogin,
      click: (item) => app.setLoginItemSettings({ openAtLogin: item.checked }),
    },
    { type: "separator" },
    { label: "Quit Carino PACS", click: quitApp },
  ]);
}

function createTray() {
  tray = new Tray(nativeImage.createFromPath(path.join(ASSETS, "tray.png")));
  tray.setToolTip("Carino PACS — DICOM store");
  tray.setContextMenu(buildMenu());
  tray.on("click", showWindow);
  tray.on("double-click", showWindow);
}

function quitApp() {
  app.isQuitting = true;
  if (py) { try { py.kill(); } catch (e) {} py = null; }
  app.quit();
}

// ---- lifecycle ---------------------------------------------------------
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showWindow);

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);

    let base = loadSavedDataDir();
    if (!base) { base = await firstRunSetup(); if (!base) { app.quit(); return; } }
    dataDir = base;
    ensureFolders(dataDir);

    createWindow();   // shows loading.html
    createTray();
    showWindow();     // visible right away — no black screen

    startEngine();
    const up = await waitForServer();
    if (up) win.loadURL(serverUrl);
    else showError("The dashboard did not respond in time. The engine may have failed to start.");
  });

  app.on("window-all-closed", () => { /* keep running in the tray */ });
  app.on("activate", showWindow);
  app.on("before-quit", () => { app.isQuitting = true; if (py) { try { py.kill(); } catch (e) {} } });
}
