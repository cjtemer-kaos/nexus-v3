# AGENTS.md — nexus-v3

Guía para agentes que trabajan en este repo. **Léela antes de tocar código.**

## Qué es
Harness de agentes profesional: loop TDAO (Think→Decide→Act→Observe) con tools,
compactación 4 capas, scope jail, checkpoints/diff, LSP, routing LLM local+cloud,
grafo de conocimiento, sub-agentes, MCP (cliente + servidor) y UI Electron+React.

## Estructura
```
src/api/server.py     FastAPI: todos los endpoints + _build_registry/_build_scope/_run_agent_once
src/core/             agent_loop, provider_router, tool_registry, scope, session_log, event_store,
                      checkpoints, diagnostics, lsp_client, board, worktree, diffutil, sub_agent_spawner,
                      compaction, hooks, learning, knowledge graph…
src/providers/        ollama, cloud (BYOK: opencode-go/groq), llamacpp, cli (claude/codex/opencode)
src/tools/            builtin, web_search, web_fetch, code_search
src/mcp_server.py     servidor MCP (inbound, stdio) que expone las tools
src/acp_server.py     adaptador ACP (Zed) sobre stdio
ui/                   React + Vite + Tailwind (Electron la embebe)
config/config.yaml    TODA la config (nada hardcodeado)
scripts/              e2e_diagnostic.py, run_ft.bat, start_hidden_ft.vbs, nexus-v3-launch.vbs
tests/                pytest (incl. test_e2e_live.py)
docs/                 CAPABILITY_MATRIX, NEXUS_ROADMAP, COMPETITOR_AUTOPSY, PI_INTEGRATION
```

## Comandos
**Modo app (real, el del acceso directo)**: `wscript scripts\nexus-v3-launch.vbs`
  → Electron levanta el backend en un **puerto libre** (efímero) y sirve `ui/dist`.
  El puerto NO es fijo: se lee de `data/backend.log` o del propio Electron.
  Log del backend: **`data/backend.log`** (ahí va stdout/stderr del server).
**Modo dev (backend + Vite + Electron)**: `npm run dev` (backend fijo :8765, Vite :5174).
- Backend solo (dev, fijo): `wscript scripts\start_hidden_ft.vbs` (puerto **8765**).
- Suite: `.venv-ft314\Scripts\python.exe -m pytest -q`
- Diagnóstico E2E: `.venv-ft314\Scripts\python.exe scripts\e2e_diagnostic.py` (y `--llm`)
- UI dev: `cd ui && npm run dev` (**5174**) · build: `cd ui && npm run build`
- Compilar rápido: `.venv-ft314\Scripts\python.exe -m py_compile <archivo>`
- Higiene (backups del brain + workspace + vendor): `.venv-ft314\Scripts\python.exe scripts\hygiene.py`
  (dry-run; `--apply` para ejecutar, `--keep N` para retención).
- Electron main: `npm run build:electron` (tsc → `electron/dist/main.js`, gitignored)

## Reglas (no negociables)
1. **Nada hardcodeado**: config env>archivo>autodetección. Usá `_load_config_safe()` /
   `load_raw_config()` (NO `load_config()` para escribir; expande `${ENV}`).
2. **Secretos** nunca al disco/repo. **No tocar** `NEXUS_BRAIN` de supernexus-v2 (v3 usa
   `NEXUS_V3_BRAIN`/`./brain`).
3. **Todo silencioso** (VBS/pythonw). El server arranca con `pythonw` + `cmd /c`.
4. **Scope jail**: toda tool FS pasa por `scope.check_path`/`check_tool`. `permission: full`
   libera; `read_only` (plan mode) bloquea mutantes; `require_confirm_mutating` exige `confirm=true`.
5. **Tools async**: registralas como `async def` (dispatch las await-ea). En `server.py` `json`
   no está importado → `import json as _json` local.
6. **Sin comentarios** salvo que aporten. **Español** en UI/mensajes.
7. Tras editar: correr `py_compile` + suite. Verificar con evidencia real (no asumir).

## Protocolo de verificación
1. `py_compile` de lo tocado.
2. `pytest -q` (exit 0).
3. `scripts/e2e_diagnostic.py` (esperado: `total=N pass=N fail=0`).
4. Para UI: build + verificar en navegador (dev :5174) con chrome-devtools.
5. Commit sólo cuando se pide. Mensaje en español, cuerpo con el "por qué".

## Puertos
Backend: **dinámico** (puerto libre) en modo app Electron; **8765** en modo dev.
UI: :5174 (dev) · :11434 ollama · :8789 STT · :8790 Studio · :8188 ComfyUI · :4096 opencode.
DBs: `data/session.db` (sesiones WAL+FTS), `data/event_store.db` (eventos), `brain/knowledge_graph.db` (grafo).

## Mapa de features
Loop TDAO · compactación · hooks (3 gates) · event-store · session log (WAL+FTS, fork/rewind/tree) ·
grafo OPCODE (learn/recall) · RAG · routing por capacidad (tools/vision) · checkpoints + diff (UI Cambios) ·
LSP (pylsp) · sub-agentes continuables · MCP cliente/servidor · ACP · board con claims · worktrees ·
plan mode · steer · sandbox (argv+política) · ayudantes/gemas · self-learning.
