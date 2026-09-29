/**
 * Nexus v3 — Electron main process (like Hermes/Goose pattern).
 * Plain JS build of main.ts — keep in sync.
 * - Single-instance lock
 * - Spawns Python harness backend (src/api/server.py) on free port, kills on window-all-closed / before-quit
 * - Tray with Show / Quit
 * - Modern security: contextIsolation, sandbox, no nodeIntegration, preload via contextBridge
 * - Dev: loads Vite http://localhost:5174; Prod: loads http://127.0.0.1:{backendPort} (FastAPI serves ui/dist)
 */
const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, dialog, shell } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const { createServer: createNetServer } = require("node:net");
const { existsSync, mkdirSync } = require("node:fs");
const { join, resolve } = require("node:path");

function getProjectRoot() {
  if (app.isPackaged) {
    const unpacked = join(process.resourcesPath, "app.asar.unpacked");
    if (existsSync(join(unpacked, "config", "config.yaml"))) return unpacked;
    if (existsSync(join(process.resourcesPath, "config", "config.yaml"))) return process.resourcesPath;
    return process.resourcesPath;
  }
  const ACTUAL_ROOT = existsSync(join(__dirname, "..", "config", "config.yaml"))
    ? resolve(__dirname, "..")
    : resolve(__dirname, "..", "..");
  const ALT_ROOT = resolve(__dirname, "..", "..");
  const ROOT_CAND = existsSync(join(ACTUAL_ROOT, "config", "config.yaml")) ? ACTUAL_ROOT : ALT_ROOT;
  return ROOT_CAND;
}
const ROOT = getProjectRoot();
const UI_DIST = join(ROOT, "ui", "dist");
const PYTHON_CANDIDATES_WIN = [
  join(ROOT, ".venv", "Scripts", "python.exe"),
  join(ROOT, ".venv", "Scripts", "pythonw.exe"),
  "python",
  "python3",
  "py",
];

let mainWindow = null;
let tray = null;
let pyProc = null;
let backendPort = 8765;
let backendUrl = "";
let isQuitting = false;
let backendReady = false;

// Single-instance lock (must be first)
const singleLock = app.requestSingleInstanceLock();
if (!singleLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

app.on("web-contents-created", (_e, contents) => {
  contents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  contents.on("will-navigate", (event, url) => {
    const allowed = url.startsWith("http://localhost:") || url.startsWith("http://127.0.0.1:") || url.startsWith("file://");
    if (!allowed && !url.startsWith("http://localhost:5174")) {
      if (backendUrl && !url.startsWith(backendUrl)) {
        event.preventDefault();
        shell.openExternal(url);
      }
    }
  });
});

function getFreePort() {
  return new Promise((res, rej) => {
    const srv = createNetServer();
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      srv.close(() => res(addr.port));
    });
    srv.on("error", rej);
  });
}

function getPythonExecutable() {
  if (process.env.NEXUS_PYTHON && process.env.NEXUS_PYTHON.trim()) return process.env.NEXUS_PYTHON.trim();
  if (process.platform === "win32") {
    for (const p of PYTHON_CANDIDATES_WIN) {
      if (p.includes("\\") || p.includes("/")) {
        if (existsSync(p)) return p;
      } else {
        return p;
      }
    }
    return "python";
  }
  const candidates = [join(ROOT, ".venv", "bin", "python"), "python3", "python"];
  for (const c of candidates) {
    if (c.includes("/")) {
      if (existsSync(c)) return c;
    } else return c;
  }
  return "python3";
}

function backendArgs(port) {
  return ["-m", "src.api.server", "--port", String(port), "--host", "127.0.0.1"];
}

async function spawnBackend(port) {
  const py = getPythonExecutable();
  const isPyLauncher = py === "py";
  const args = isPyLauncher ? ["-3", ...backendArgs(port)] : backendArgs(port);
  const env = { ...process.env, PYTHONUNBUFFERED: "1" };
  delete env.ELECTRON_RUN_AS_NODE;
  const dataDir = join(ROOT, "data");
  try { mkdirSync(dataDir, { recursive: true }); } catch {}
  console.log(`[main] spawning backend: ${py} ${args.join(" ")} (cwd=${ROOT})`);
  const proc = spawn(py, args, {
    cwd: ROOT,
    env,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
    detached: false,
  });
  proc.stdout?.on("data", (d) => { const s = d.toString().trim(); if (s) console.log(`[py] ${s}`); });
  proc.stderr?.on("data", (d) => { const s = d.toString().trim(); if (s) console.warn(`[py:err] ${s}`); });
  proc.on("error", (err) => {
    console.error(`[main] backend spawn error: ${err.message}`);
    dialog.showErrorBox("Nexus v3 — Backend error", `No se pudo iniciar el backend Python.\n\nComando: ${py} ${args.join(" ")}\nError: ${err.message}\n\nVerifica que Python 3.13+ este instalado.`);
  });
  proc.on("exit", (code, signal) => {
    console.log(`[main] backend exit code=${code} signal=${signal} quitting=${isQuitting}`);
    if (!isQuitting && code !== 0 && code !== null) {
      backendReady = false;
      mainWindow?.webContents.send("nexus:backendStatus", { ok: false, url: backendUrl });
    }
  });
  return proc;
}

async function killBackend() {
  if (!pyProc || pyProc.killed) return;
  const pid = pyProc.pid;
  console.log(`[main] killing backend pid=${pid}`);
  isQuitting = true;
  try { pyProc.kill("SIGTERM"); } catch {}
  await new Promise((resolve) => {
    const t = setTimeout(() => resolve(), 1500);
    pyProc.once("exit", () => { clearTimeout(t); resolve(); });
  });
  if (pyProc && !pyProc.killed && pyProc.pid) {
    try {
      if (process.platform === "win32") {
        spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true });
      } else {
        pyProc.kill("SIGKILL");
      }
    } catch {}
  }
  pyProc = null;
}

async function waitForBackend(url, timeoutMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(2000) });
      if (res.ok) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

function getIconPath() {
  const candidates = [
    join(ROOT, "build", "icon.png"),
    join(ROOT, "build", "icon.ico"),
    join(ROOT, "ui", "public", "vite.svg"),
    join(UI_DIST, "vite.svg"),
  ];
  for (const p of candidates) if (existsSync(p)) return p;
  return undefined;
}

function createTray() {
  if (tray) return;
  const iconPath = getIconPath();
  let img;
  if (iconPath) {
    img = nativeImage.createFromPath(iconPath);
    if (img.isEmpty()) img = undefined;
  }
  if (!img || img.isEmpty()) img = nativeImage.createEmpty();
  tray = new Tray(img);
  const menu = Menu.buildFromTemplate([
    { label: "Mostrar Nexus v3", click: () => { mainWindow?.show(); mainWindow?.focus(); } },
    { label: `Backend: ${backendUrl || "iniciando..."}`, enabled: false },
    { type: "separator" },
    { label: "Reiniciar backend", click: async () => { await restartBackend(); } },
    { label: "Abrir en navegador", click: () => { if (backendUrl) shell.openExternal(backendUrl); } },
    { type: "separator" },
    { label: "Salir", click: () => { isQuitting = true; app.quit(); } },
  ]);
  tray.setToolTip("Nexus v3 — Arnes");
  tray.setContextMenu(menu);
  tray.on("double-click", () => { mainWindow?.show(); mainWindow?.focus(); });
}

async function restartBackend() {
  try {
    if (pyProc) await killBackend();
    backendPort = await getFreePort();
    backendUrl = `http://127.0.0.1:${backendPort}`;
    pyProc = await spawnBackend(backendPort);
    const ok = await waitForBackend(backendUrl, 20000);
    backendReady = ok;
    mainWindow?.webContents.send("nexus:backendStatus", { ok, url: backendUrl });
    if (tray) {
      const menu = Menu.buildFromTemplate([
        { label: "Mostrar Nexus v3", click: () => { mainWindow?.show(); mainWindow?.focus(); } },
        { label: `Backend: ${backendUrl}`, enabled: false },
        { type: "separator" },
        { label: "Reiniciar backend", click: async () => { await restartBackend(); } },
        { label: "Abrir en navegador", click: () => { if (backendUrl) shell.openExternal(backendUrl); } },
        { type: "separator" },
        { label: "Salir", click: () => { isQuitting = true; app.quit(); } },
      ]);
      tray.setContextMenu(menu);
    }
    return { ok, url: backendUrl };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

function createWindow() {
  const preloadPath = join(__dirname, "preload.js");
  const iconPath = getIconPath();
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 600,
    show: false,
    backgroundColor: "#09090b",
    title: "Nexus v3 — Arnes",
    icon: iconPath,
    webPreferences: {
      preload: existsSync(preloadPath) ? preloadPath : undefined,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
    autoHideMenuBar: true,
  });

  const loadingHtml = `
    <html><head><meta charset="utf-8"><style>
      *{margin:0;padding:0;box-sizing:border-box}
      body{background:#09090b;color:#e4e4e7;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:16px}
      .dot{width:10px;height:10px;border-radius:999px;background:#8b5cf6;animation:pulse 1.2s infinite}
      @keyframes pulse{0%,100%{opacity:0.4;transform:scale(0.9)}50%{opacity:1;transform:scale(1)}}
      .muted{color:#71717a;font-size:12px}
      code{background:rgba(255,255,255,0.06);padding:2px 6px;border-radius:6px;font-size:12px}
    </style></head><body>
      <div class="dot"></div>
      <div>Iniciando arnes Nexus v3...</div>
      <div class="muted">Backend: <code id="u">${backendUrl || ""}</code></div>
      <div class="muted">Si tarda, revisa que Python 3.13+ y dependencias esten instaladas.</div>
    </body></html>
  `;
  mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(loadingHtml)}`);
  mainWindow.once("ready-to-show", () => mainWindow?.show());
  mainWindow.on("close", (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow?.hide();
      tray?.displayBalloon?.({ title: "Nexus v3", content: "Sigue ejecutandose en la bandeja. Click para mostrar." });
    }
  });
  mainWindow.webContents.on("render-process-gone", (_e, details) => {
    console.error("[main] render-process-gone", details);
  });
  return mainWindow;
}

async function start() {
  try {
    backendPort = await getFreePort();
  } catch {
    backendPort = 8765;
  }
  backendUrl = `http://127.0.0.1:${backendPort}`;
  console.log(`[main] backendUrl=${backendUrl} root=${ROOT}`);

  ipcMain.handle("nexus:getBackendUrl", () => backendUrl);
  ipcMain.handle("nexus:getBackendPort", () => backendPort);
  ipcMain.handle("nexus:restartBackend", async () => restartBackend());

  pyProc = await spawnBackend(backendPort);
  createWindow();
  createTray();
  const ok = await waitForBackend(backendUrl, 25000);
  backendReady = ok;
  console.log(`[main] backend ready=${ok}`);
  if (!mainWindow) return;
  if (ok) {
    const isDev = !app.isPackaged && process.env.ELECTRON_IS_DEV !== "0";
    let target;
    if (isDev) {
      try {
        const r = await fetch("http://localhost:5174/", { signal: AbortSignal.timeout(1500) });
        target = r.ok ? "http://localhost:5174/" : backendUrl;
      } catch {
        target = backendUrl;
      }
    } else {
      target = backendUrl;
    }
    console.log(`[main] loading ${target}`);
    await mainWindow.loadURL(target);
  } else {
    dialog.showMessageBox(mainWindow, { type: "warning", title: "Nexus v3 — Backend offline", message: `Backend no disponible en ${backendUrl}.\nVerifica Python y dependencias.\nIntenta Reiniciar backend desde la bandeja.` });
  }
  mainWindow.webContents.send("nexus:backendStatus", { ok, url: backendUrl });
  mainWindow.show();
  mainWindow.focus();
}

app.whenReady().then(start);
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
  else mainWindow?.show();
});
app.on("window-all-closed", async () => {
  console.log("[main] window-all-closed — killing backend and quitting");
  await killBackend();
  app.quit();
});
app.on("before-quit", async (e) => {
  if (pyProc && !isQuitting) {
    e.preventDefault();
    isQuitting = true;
    await killBackend();
    app.quit();
  }
});
process.on("exit", () => { try { pyProc?.kill(); } catch {} });
process.on("SIGINT", async () => { await killBackend(); process.exit(0); });
process.on("SIGTERM", async () => { await killBackend(); process.exit(0); });
