# Nexus v3 â€” Harness Profesional para Agentes Autonomos

**Cerebro:** Knowledge Graph + RAG + memoria jerarquica (local `./brain`, portable, sin vendor lock-in)
**Harness:** Agent loop + ToolRegistry + Scope + Swap on-demand + Hooks + Compaction (inspirado en DeepSeek Harness, Hermes, OpenCode)
**Desktop:** Electron 32 â€” main spawns Python harness (`src/api/server.py`) on free port, kills on `window-all-closed`, single-instance lock, tray, NSIS installer

## Estructura
- `src/core/` â€” harness (session, agent-loop, tools, scope, compaction, hooks, event-store, mcp-client)
- `src/brain/` â€” cerebro local (`./brain/knowledge_graph.db` + `rag_index.db`) â€” portable, configurable via `NEXUS_V3_BRAIN`
- `src/bridges/mcp_bridge_server.py` â€” MCP bridge (expone tools del harness por MCP); `src/core/mcp_client.py` â€” cliente MCP (consume servidores del bundle)
- `src/api/server.py` â€” FastAPI harness backend (sirve `ui/dist` + `/api/*`)
- `electron/` â€” Electron main + preload (TypeScript, contextIsolation, sandbox, single-instance, tray)
- `ui/` â€” React 19 + Vite + Tailwind + shadcn
- `providers/` â€” Ollama, llama.cpp, nube (auto-detect, API key programable)
- `_attic/` â€” cÃ³digo muerto de v2 movido aquÃ­ (no se trackea; reversible)

Nada harcodeado, todo vÃ­a `config.yaml` + env, distribuible.

## Estado (auditorÃ­a 2026-10-11)
Ver `AUDIT.md`. Resumen: el harness del blueprint estÃ¡ **cableado en producciÃ³n** (compaction + hooks + event-store + gate de seguridad load-bearing + validaciÃ³n de argumentos + log por call + MCP cliente) y se purgÃ³ el cÃ³digo muerto (178 mÃ³dulos de v2 â†’ `_attic/`). Suite: **288 tests** + 6 evals, verde.

## InstalaciÃ³n

### Windows â€” un solo paso

1. ClonÃ¡ el repo (o extraÃ© el ZIP) en una carpeta **sin espacios ni OneDrive**.
2. Doble clic en **`INSTALAR.bat`**.
3. Al terminar, abrÃ­ **Nexus v3** desde el acceso directo del escritorio (o `nexus-v3.cmd`).

`INSTALAR.bat` â†’ `bootstrap.ps1` hace todo y verifica el resultado:

| Paso | QuÃ© hace |
|---|---|
| 1 | Comprueba git / Node â‰¥ 18 / Python 3.11â€“3.13 |
| 2 | Crea el entorno `.venv-ft314` (idempotente) |
| 3 | `pip install -r requirements.txt` + un `uv` propio (para los MCP que usan `uvx`) |
| 4 | `npm ci` en la raÃ­z y en `ui/` |
| 5 | Compila **Electron** y la **UI** (`electron/dist` + `ui/dist`) |
| 6 | Crea el grafo e importa la **base de conocimiento compartida** (`brain/seed/*.nexuskg.json[.gz]`) |
| 7 | Instala/verifica los MCP del bundle (`data/mcp_bundle/manifest.json`) |
| 8 | Corre `scripts/verify_install.py` y crea los accesos directos |

Si algo falla, el paso exacto queda en **`INSTALAR-LOG.txt`**; se puede volver a ejecutar y retoma.

### InstalaciÃ³n manual (Linux/macOS o si preferÃ­s a mano)

```bash
python -m venv .venv-ft314
.venv-ft314/Scripts/python.exe -m pip install -r requirements.txt   # Linux: .venv-ft314/bin/python
.venv-ft314/Scripts/python.exe -m pip install uv
npm ci && npm ci --prefix ui
npm run build                       # electron + ui
.venv-ft314/Scripts/python.exe scripts/bootstrap_brain.py
.venv-ft314/Scripts/python.exe scripts/setup_mcp.py --fix
.venv-ft314/Scripts/python.exe scripts/verify_install.py
```

### Verificar una instalaciÃ³n

```bash
.venv-ft314/Scripts/python.exe scripts/verify_install.py
```

Comprueba dependencias, artefactos de UI/Electron, grafo con la base compartida, bundle MCP y
**arranca el servidor MCP propio** (`python -m src.mcp_server`) para hacer
`initialize` + `tools/list` + `recall`. Exit 0 = instalaciÃ³n completa.

### MCP

```bash
python scripts/setup_mcp.py                # estado por servidor
python scripts/setup_mcp.py --fix          # instala lo que falte
python scripts/setup_mcp.py --emit-client  # config lista para opencode/Claude/Cursor
```

El estado queda en `data/mcp_bundle/status.json`. Las tools marcadas como lectura en el
manifest o en `approval.mcp_read_only` (config) corren solas; el resto pide aprobaciÃ³n en
la UI y falla cerrado en contextos sin canal (servidor MCP, subagentes).

Estados posibles: `ok` (listo), `missing` (falta el binario), `needs_key` (falta tu clave),
`manual` (requiere instalaciÃ³n a mano), `disabled` (apagado por defecto) y `remote`
(configurado, pero el cliente MCP interno solo lanza servidores **stdio**: los remotos por
HTTP se usan desde el cliente que ya los tenga, p. ej. opencode).

`uvx`: si el del sistema falla (tÃ­pico `os error 448` en Windows), el bootstrap instala un
`uv` propio en el venv. Precedencia al resolver binarios: `NEXUS_UVX`/`NEXUS_NPX` â†’
venv del proyecto â†’ `PATH`.

### Grafo / conocimiento

- Base del sistema: `brain/seed/nexus-base.nexuskg.json.gz` (versionada, saneada: sin secretos, IPs, emails ni rutas de una PC).
- Semilla curada: `brain/seed/nexus-seed.nexuskg.json`.
- Se importan todas con merge por `topic_key`; cada usuario sigue sumando lo suyo. Detalle en `brain/README.md`.
- Exportar/importar entre mÃ¡quinas: `/api/knowledge/export` â†’ `.nexuskg.json` â†’
  `POST /api/knowledge/import` (merge por `topic_key`, dry-run por defecto).
- Los `*.db` no se versionan: se generan en runtime (ver `brain/README.md`).


## Desktop App (Electron)

Modern Electron best practices (contextIsolation, sandbox, preload contextBridge, no nodeIntegration):

```bash
# Dev â€” harness :8765 + Vite :5174 + Electron (con concurrently)
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

# Run prod (sin instalador) â€” build + Electron cargando http://127.0.0.1:{port}
npm start

# Electron directo (dev, usa Vite si estÃ¡ en :5174, si no fallback a harness)
npm run electron              # usa electron/dist (tsc)
npm run electron:ts           # tsc + run

# Installer (NSIS x64 + zip) -> release/
npm run dist
npm run dist:dir              # unpacked dir para test rÃ¡pido
```

### Comportamiento (como Hermes/Goose)
- **Single-instance lock:** segunda instancia enfoca la ventana existente
- **Spawn backend:** puerto libre aleatorio, `python -m src.api.server --port {port}`, cwd=project root, PYTHONUNBUFFERED=1
- **Kill on window-all-closed:** `window-all-closed` + `before-quit` + `SIGTERM/SIGINT` + `taskkill /T /F` en Windows para el Ã¡rbol entero
- **Tray:** Mostrar, Reiniciar backend, Abrir en navegador, Salir
- **Security:** `contextIsolation:true, sandbox:true, nodeIntegration:false`, `preload.js` vÃ­a `contextBridge`, `webSecurity:true`, `will-navigate` bloqueado a externo (shell.openExternal)
- **Backend discovery:** `window.nexus.getBackendUrl()` / `getBackendPort()` vÃ­a IPC, `ui/src/lib/api.ts` usa eso o fallback a `http://127.0.0.1:8765` / Vite proxy
- **Python discovery:** `NEXUS_PYTHON` env wins, luego `.venv/Scripts/python.exe`, luego `python`/`py -3` en PATH
- **Icon:** `build/icon.ico` + `build/icon.png` (256) â€” usado por BrowserWindow y Tray y electron-builder

### Config
Ver `config/config.yaml` y `config/config.schema.json`. Puerto del harness lo elige Electron (free port); el backend tambiÃ©n acepta `--port 0` (random).

## Actualizaciones

La app se actualiza sola al abrirse: antes de arrancar el backend hace
`git fetch` + `git pull --ff-only` de su rama y recompila `ui/dist` y
`electron/dist` solo si cambio algo ahi. Si el arbol tiene cambios locales o
commits sin publicar no toca nada, y deja el detalle en `data/self_update.log`.
Estado y boton "actualizar ahora" en Ajustes > Resumen.