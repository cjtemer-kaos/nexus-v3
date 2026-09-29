# Nexus v3 — Harness Profesional para Agentes Autonomos

**Cerebro:** Knowledge Graph + RAG + memoria jerarquica (local `./brain`, portable, sin vendor lock-in)
**Harness:** Agent loop + ToolRegistry + Scope + Swap on-demand + Hooks + Compaction (inspirado en DeepSeek Harness, Hermes, OpenCode)
**Desktop:** Electron 32 — main spawns Python harness (`src/api/server.py`) on free port, kills on `window-all-closed`, single-instance lock, tray, NSIS installer

## Estructura
- `src/core/` — harness (session, agent-loop, tools, scope, compaction)
- `src/brain/` — cerebro local (`./brain/knowledge_graph.db` + `rag_index.db`) — portable, configurable via `NEXUS_BRAIN`
- `src/mcp/` — MCP bridge para PC y agentes
- `src/api/server.py` — FastAPI harness backend (sirve `ui/dist` + `/api/*`)
- `electron/` — Electron main + preload (TypeScript, contextIsolation, sandbox, single-instance, tray)
- `ui/` — React 19 + Vite + Tailwind + shadcn
- `providers/` — Ollama, llama.cpp, nube (auto-detect, API key programable)

Nada harcodeado, todo vía `config.yaml` + env, distribuible.

## Desktop App (Electron)

Modern Electron best practices (contextIsolation, sandbox, preload contextBridge, no nodeIntegration):

```bash
# Dev — harness :8765 + Vite :5174 + Electron (con concurrently)
npm run dev

# Solo backend (FastAPI sirve ui/dist en prod, o proxy en dev)
python -m src.api.server --port 8765
# o
python start_server.py
python start_server.py --port 8765

# Solo UI dev
npm run dev:vite   # == npm run dev --prefix ui

# Build UI + Electron TS
npm run build

# Run prod (sin instalador) — build + Electron cargando http://127.0.0.1:{port}
npm start

# Electron directo (dev, usa Vite si está en :5174, si no fallback a harness)
npm run electron              # usa electron/dist (tsc)
npm run electron:ts           # tsc + run

# Installer (NSIS x64 + zip) -> release/
npm run dist
npm run dist:dir              # unpacked dir para test rápido
```

### Comportamiento (como Hermes/Goose)
- **Single-instance lock:** segunda instancia enfoca la ventana existente
- **Spawn backend:** puerto libre aleatorio, `python -m src.api.server --port {port}`, cwd=project root, PYTHONUNBUFFERED=1
- **Kill on window-all-closed:** `window-all-closed` + `before-quit` + `SIGTERM/SIGINT` + `taskkill /T /F` en Windows para el árbol entero
- **Tray:** Mostrar, Reiniciar backend, Abrir en navegador, Salir
- **Security:** `contextIsolation:true, sandbox:true, nodeIntegration:false`, `preload.js` vía `contextBridge`, `webSecurity:true`, `will-navigate` bloqueado a externo (shell.openExternal)
- **Backend discovery:** `window.nexus.getBackendUrl()` / `getBackendPort()` vía IPC, `ui/src/lib/api.ts` usa eso o fallback a `http://127.0.0.1:8765` / Vite proxy
- **Python discovery:** `NEXUS_PYTHON` env wins, luego `.venv/Scripts/python.exe`, luego `python`/`py -3` en PATH
- **Icon:** `build/icon.ico` + `build/icon.png` (256) — usado por BrowserWindow y Tray y electron-builder

### Config
Ver `config/config.yaml` y `config/config.schema.json`. Puerto del harness lo elige Electron (free port); el backend también acepta `--port 0` (random).

