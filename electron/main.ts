/**
 * Nexus v3 — Electron main process (like Hermes/Goose pattern).
 *
 * - Single-instance lock
 * - Spawns Python harness backend (src/api/server.py) on free port, kills on window-all-closed / before-quit
 * - Tray with Show / Quit
 * - Modern security: contextIsolation, sandbox, no nodeIntegration, preload via contextBridge
 * - Dev: loads Vite http://localhost:5174; Prod: loads http://127.0.0.1:{backendPort} (FastAPI serves ui/dist)
 * - Health polling + loading screen + graceful backend lifecycle
 *
 * Build: npm run build:electron  (tsc -p electron/tsconfig.json)
 * Run:  npm run dev              (starts backend + vite + electron)
 *       npm start                (prod — backend + electron loading SPA from backend)
 */

import { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, dialog, shell } from "electron";
import { spawn, ChildProcess } from "node:child_process";
import { createServer as createNetServer } from "node:net";
import { existsSync, mkdirSync, createWriteStream, type WriteStream } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// ——————————————————————————————————————————————
// Paths & constants
// ——————————————————————————————————————————————
function getProjectRoot(): string {
  // Packaged app: src is unpacked to app.asar.unpacked
  if (app.isPackaged) {
    const unpacked = join(process.resourcesPath, "app.asar.unpacked");
    if (existsSync(join(unpacked, "config", "config.yaml"))) return unpacked;
    // Fallback: extraResources at resources root
    if (existsSync(join(process.resourcesPath, "config", "config.yaml"))) return process.resourcesPath;
    // Last fallback: app path
    return process.resourcesPath;
  }
  // Dev: electron/dist -> ../../ = project root
  const PROJECT_ROOT = resolve(__dirname, "..", "..");
  const ACTUAL_ROOT = existsSync(join(__dirname, "..", "..", "config", "config.yaml"))
    ? resolve(__dirname, "..", "..")
    : resolve(__dirname, "..");
  const ROOT = existsSync(join(ACTUAL_ROOT, "config", "config.yaml")) ? ACTUAL_ROOT : PROJECT_ROOT;
  return ROOT;
}

// Lazy — computed after app ready? But app.isPackaged available immediately
const ROOT = getProjectRoot();
const UI_DIST = join(ROOT, "ui", "dist");
const PYTHON_CANDIDATES_WIN = [
  join(ROOT, ".venv-ft314", "Scripts", "python.exe"),
  join(ROOT, ".venv-ft314", "Scripts", "pythonw.exe"),
  join(ROOT, ".venv", "Scripts", "python.exe"),
  join(ROOT, ".venv", "Scripts", "pythonw.exe"),
  "python",
  "python3",
  "py",
];

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let pyProc: ChildProcess | null = null;
let backendPort: number = 8765;
let backendUrl: string = "";
let isQuitting = false;
let backendReady = false;

// ——————————————————————————————————————————————
// Backend log → data/backend.log (el stdout de Electron se PIERDE al abrir por
// acceso directo; sin esto no hay evidencia de por qué el backend no levanta).
// ——————————————————————————————————————————————
let _blogStream: WriteStream | null = null;
function blog(tag: string, s: string): void {
  const line = `${new Date().toISOString()} ${tag} ${s}`;
  console.log(line);
  try {
    if (!_blogStream) {
      const dir = join(ROOT, "data");
      try { mkdirSync(dir, { recursive: true }); } catch {}
      _blogStream = createWriteStream(join(dir, "backend.log"), { flags: "a" });
    }
    _blogStream.write(line + "\n");
  } catch { /* noop */ }
}

// ——————————————————————————————————————————————
// Single-instance lock (must be first)
// ——————————————————————————————————————————————
const singleLock = app.requestSingleInstanceLock();
if (!singleLock) {
  app.quit();
} else {
  app.on("second-instance", async () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
    // Auto-sanacion: si la instancia viva NO tiene backend, respawnearlo. Sin esto,
    // el acceso directo solo enfoca una ventana existente (p.ej. con backend muerto).
    let alive = backendReady;
    if (alive) {
      try {
        const r = await fetch(`${backendUrl}/api/health`, { signal: AbortSignal.timeout(2500) });
        alive = r.ok;
      } catch { alive = false; }
    }
    if (!alive) {
      blog("[main]", "second-instance: backend no vivo -> restartBackend()");
      const res = await restartBackend();
      if (res.ok && mainWindow) await mainWindow.loadURL(backendUrl);
    }
  });
}

// ——————————————————————————————————————————————
// Security: harden
// ——————————————————————————————————————————————
app.on("web-contents-created", (_e, contents) => {
  contents.setWindowOpenHandler(({ url }) => {
    // Open external links in system browser, not new Electron window
    shell.openExternal(url);
    return { action: "deny" };
  });
  contents.on("will-navigate", (event, url) => {
    const allowed = url.startsWith("http://localhost:") || url.startsWith("http://127.0.0.1:") || url.startsWith("file://");
    if (!allowed && !url.startsWith("http://localhost:5174")) {
      // Allow backend and vite; block everything else from in-app navigation
      // But allow SPA routes: same origin only
      if (!url.startsWith(backendUrl) && backendUrl) {
        event.preventDefault();
        shell.openExternal(url);
      }
    }
  });
});

// ——————————————————————————————————————————————
// Helpers: free port, python discovery, logging
// ——————————————————————————————————————————————
function getFreePort(): Promise<number> {
  return new Promise((res, rej) => {
    const srv = createNetServer();
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address() as { port: number };
      srv.close(() => res(addr.port));
    });
    srv.on("error", rej);
  });
}

function getPythonExecutable(): string {
  // Env override wins (NEXUS_PYTHON)
  if (process.env.NEXUS_PYTHON && process.env.NEXUS_PYTHON.trim()) return process.env.NEXUS_PYTHON.trim();
  if (process.platform === "win32") {
    for (const p of PYTHON_CANDIDATES_WIN) {
      if (p.includes("\\") || p.includes("/")) {
        if (existsSync(p)) return p;
      } else {
        // bare command — trust PATH, return first bare
        return p;
      }
    }
    return "python";
  }
  // mac/linux
  const candidates = [join(ROOT, ".venv", "bin", "python"), "python3", "python"];
  for (const c of candidates) {
    if (c.includes("/")) {
      if (existsSync(c)) return c;
    } else return c;
  }
  return "python3";
}

function backendArgs(port: number): string[] {
  return ["-m", "src.api.server", "--port", String(port), "--host", "127.0.0.1"];
}

async function spawnBackend(port: number): Promise<ChildProcess> {
  const py = getPythonExecutable();
  // On Windows, `py` needs `-3` to ensure Python 3
  const isPyLauncher = py === "py";
  const args = isPyLauncher ? ["-3", ...backendArgs(port)] : backendArgs(port);

  const env = {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    // Preserve NEXUS_BRAIN if set; backend's link.py uses it
    ELECTRON_RUN_AS_NODE: undefined,
  } as NodeJS.ProcessEnv;

  // Ensure data dir exists for logs
  const dataDir = join(ROOT, "data");
  try { mkdirSync(dataDir, { recursive: true }); } catch {}

  blog("[main]", `spawning backend: ${py} ${args.join(" ")} (cwd=${ROOT})`);

  // On Windows, use shell:false to allow proper kill; use detached:false so we own the process tree
  const proc = spawn(py, args, {
    cwd: ROOT,
    env,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
    detached: false,
  });

  proc.stdout?.on("data", (d: Buffer) => {
    const s = d.toString().trim();
    if (s) blog("[py]", s);
  });
  proc.stderr?.on("data", (d: Buffer) => {
    const s = d.toString().trim();
    if (s) blog("[py:err]", s);
  });
  proc.on("error", (err) => {
    blog("[main]", `backend spawn error: ${err.message}`);
    dialog.showErrorBox("Nexus v3 — Backend error", `No se pudo iniciar el backend Python.\n\nComando: ${py} ${args.join(" ")}\nError: ${err.message}\n\nVerifica que Python 3.13+ esté instalado y que 'pip install -r requirements.txt' se haya ejecutado (si aplica).`);
  });
  proc.on("exit", (code, signal) => {
    blog("[main]", `backend exit code=${code} signal=${signal} quitting=${isQuitting}`);
    if (!isQuitting && code !== 0 && code !== null) {
      // Unexpected crash — notify but don't auto-restart aggressively
      backendReady = false;
      mainWindow?.webContents.send("nexus:backendStatus", { ok: false, url: backendUrl });
    }
  });

  return proc;
}

async function killBackend(): Promise<void> {
  if (!pyProc || pyProc.killed) return;
  const pid = pyProc.pid;
  blog("[main]", `killing backend pid=${pid}`);
  isQuitting = true;

  // Best-effort graceful
  try { pyProc.kill("SIGTERM"); } catch {}

  // Wait briefly
  await new Promise<void>((resolve) => {
    const t = setTimeout(() => resolve(), 1500);
    pyProc!.once("exit", () => { clearTimeout(t); resolve(); });
  });

  // Force kill if still alive (Windows taskkill for tree)
  if (pyProc && !pyProc.killed && pyProc.pid) {
    try {
      if (process.platform === "win32") {
        // Kill process tree on Windows
        const { spawnSync } = await import("node:child_process");
        spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true });
      } else {
        pyProc.kill("SIGKILL");
      }
    } catch {}
  }
  pyProc = null;
}

async function waitForBackend(url: string, timeoutMs = 15000): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(5000) });
      if (res.ok) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

// ——————————————————————————————————————————————
// Window & tray
// ——————————————————————————————————————————————
function getIconPath(): string | undefined {
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
  let img: Electron.NativeImage | undefined;
  if (iconPath) {
    img = nativeImage.createFromPath(iconPath);
    if (img.isEmpty()) img = undefined;
  }
  // Fallback: 16x16 empty
  if (!img || img.isEmpty()) {
    img = nativeImage.createEmpty();
  }
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

async function restartBackend(): Promise<{ ok: boolean; url?: string; error?: string }> {
  try {
    if (pyProc) await killBackend();
    backendPort = await getFreePort();
    backendUrl = `http://127.0.0.1:${backendPort}`;
    pyProc = await spawnBackend(backendPort);
    const ok = await waitForBackend(backendUrl, 60000);
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
    backgroundColor: "#15171B",
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

  // Loading placeholder until backend ready
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
      <script>try{window.nexus?.getBackendUrl().then(u=>{const e=document.getElementById('u');if(e) e.textContent=u;});}catch{}</script>
    </body></html>
  `;

  mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(loadingHtml)}`);

  mainWindow.once("ready-to-show", () => {
    // En pantallas con escalado (125/150%) el ancho en CSS queda por debajo de
    // 900px y la topbar/composer no entran. Maximizar usa el work area real.
    mainWindow?.maximize();
    mainWindow?.show();
  });

  mainWindow.on("close", (e) => {
    // Minimize to tray instead of quit when closing window (optional — like Goose/Hermes)
    // For "proper program" we keep tray: hide to tray on close, quit via tray or app quit
    if (!isQuitting) {
      e.preventDefault();
      mainWindow?.hide();
      // Show tray hint once
      tray?.displayBalloon?.({ title: "Nexus v3", content: "Sigue ejecutandose en la bandeja. Click para mostrar." } as any);
    }
  });

  // Handle renderer crashes
  mainWindow.webContents.on("render-process-gone", (_e, details) => {
    console.error("[main] render-process-gone", details);
  });

  // Zoom por teclado (Ctrl+= / Ctrl+- / Ctrl+0). Electron NO lo trae por defecto sin
  // un menu con esos roles, por eso andaba en la version web y no en la app.
  mainWindow.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown" || !input.control) return;
    const wc = mainWindow?.webContents;
    if (!wc) return;
    const k = input.key;
    if (k === "=" || k === "+" || k === "Add") { wc.setZoomLevel(wc.getZoomLevel() + 0.5); event.preventDefault(); }
    else if (k === "-" || k === "_" || k === "Subtract") { wc.setZoomLevel(wc.getZoomLevel() - 0.5); event.preventDefault(); }
    else if (k === "0") { wc.setZoomLevel(0); event.preventDefault(); }
  });

  return mainWindow;
}

// ——————————————————————————————————————————————
// App lifecycle
// ——————————————————————————————————————————————
async function start() {
  // Must run before creating window: find free port and spawn backend
  try {
    backendPort = await getFreePort();
  } catch {
    backendPort = 8765;
  }
  backendUrl = `http://127.0.0.1:${backendPort}`;
  blog("[main]", `backendUrl=${backendUrl} root=${ROOT}`);

  // IPC before spawn so renderer can query even during loading
  ipcMain.handle("nexus:getBackendUrl", () => backendUrl);
  ipcMain.handle("nexus:getBackendPort", () => backendPort);
  ipcMain.handle("nexus:restartBackend", async () => restartBackend());

  // Spawn backend BEFORE window so health check can run in parallel
  pyProc = await spawnBackend(backendPort);

  createWindow();
  createTray();

  // Poll backend readiness, then navigate to correct URL
  const ok = await waitForBackend(backendUrl, 60000);
  backendReady = ok;
  blog("[main]", `backend ready=${ok}`);

  if (!mainWindow) return;

  if (ok) {
    const isDev = !app.isPackaged && process.env.ELECTRON_IS_DEV !== "0";
    // In dev, prefer Vite dev server if it's up; otherwise use backend URL
    let target: string;
    if (isDev) {
      // Probe Vite
      try {
        const r = await fetch("http://localhost:5174/", { signal: AbortSignal.timeout(1500) });
        target = r.ok ? "http://localhost:5174/" : backendUrl;
      } catch {
        target = backendUrl;
      }
    } else {
      target = backendUrl;
    }
    blog("[main]", `loading ${target}`);
    await mainWindow.loadURL(target);
  } else {
    const msg = `Backend no disponible en ${backendUrl}.\nVerifica que Python este instalado y que las dependencias esten instaladas.\n\nIntenta Reiniciar backend desde la bandeja.`;
    dialog.showMessageBox(mainWindow, { type: "warning", title: "Nexus v3 — Backend offline", message: msg });
  }

  mainWindow.webContents.send("nexus:backendStatus", { ok, url: backendUrl });
  mainWindow.show();
  mainWindow.focus();
}

app.whenReady().then(start);

// macOS: recreate window on dock click
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
  else mainWindow?.show();
});

// Window-all-closed: kill backend and quit (like Hermes/Goose — proper program, not daemon)
app.on("window-all-closed", async () => {
  blog("[main]", "window-all-closed — killing backend and quitting");
  await killBackend();
  // On macOS, keep app alive only if tray wants it; here we follow Hermes: quit when last window closed unless tray keeps alive
  // Requirement: "kills it on window-all-closed" — so quit unconditionally
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

// Ensure backend killed on unexpected exit
process.on("exit", () => {
  try { pyProc?.kill(); } catch {}
});
process.on("SIGINT", async () => { await killBackend(); process.exit(0); });
process.on("SIGTERM", async () => { await killBackend(); process.exit(0); });

// Single-instance already handled at top
