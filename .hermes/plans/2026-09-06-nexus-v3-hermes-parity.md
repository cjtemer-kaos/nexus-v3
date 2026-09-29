# Nexus v3 → Paridad Hermes Desktop / OpenCode Desktop — Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Convertir Nexus v3 de prototipo validado (111/111 tests mock) en asistente IA completo, funcional e independiente al nivel Hermes Desktop / OpenCode Desktop — E2E real con Zen/Go, tools Scope-guarded, MCP/gemas, UI Electron sin placeholders, brain/RAG y distribución hardened.

**Architecture:** Mantener harness actual (FastAPI `src/api/server.py` + Electron `electron/main.ts` spawnea Python) pero endurecer 6 capas: 1) backbone E2E probado contra proveedores reales, 2) resiliencia intra-cloud (fallback entre free models verificados), 3) MCP+skills/gemas, 4) UI contratos reales auditados con devtools, 5) memoria jerárquica+RAG bajo carga, 6) artefacto distribuible offline. Principios: DRY/YAGNI/TDD, sin hardcodear, todo via `config/config.yaml`+env, `NEXUS_ROOT` relativo.

**Tech Stack:** Python 3.11 FastAPI+uvicorn+httpx, Electron 30+preload contextBridge, React 19+Vite 6+zustand+Radix, SQLite WAL+FTS5, llama.cpp vendored `vendor/llama.cpp`, nomic-embed-text 768d, Zen OpenAI-compatible `https://opencode.ai/zen`.

---

## Diagnóstico (qué falta para ser Hermes/OpenCode)

- **E2E nunca probado real:** `111/111` son mocks en memoria; `/api/chat` crea `ProviderRouter`+`AgentLoop`+`ToolRegistry` (echo/read_file/write_file/list_dir/exec) pero ningún test ejercita `cloud_provider → Zen` con tool-calling real. Si Zen 503 en medio del loop, muere sin retry intra-cloud.
- **Cloud verify reciente pero incoherente:** `data/cloud_free_cache.json` 7 raw, `verify=true` 6 verified (400=permanent, 429/500/timeout=transient keep) correcto, pero `/api/providers` registra solo 2-3 models del `config.yaml` (`registered:3` vs `free_models:6`). UI `ModelSelector` no ve el pool descubierto.
- **MCP vacío:** `src/mcp/__init__.py` 0 LOC. Hermes tiene 38 tools MCP + bridge; OpenCode TUI expone skills. Nexus no puede cargar skills/gemas externas.
- **UI placeholders:** `App.tsx` topbar/search/bell son dummy; `ChatPanel/FileEditor/TerminalPanel/GraphView/ProvidersPanel` no auditados con devtools; `api.ts` `chatApi` 90s timeout genérico; ningún test E2E devtools.
- **Brain/RAG sin carga real:** `brain/*.db` + `SessionLog WAL+FTS` existen, `rag.py/embeddings.py` sin ingesta verificada, compaction L1-L4 probada solo mock.
- **Distribución semi-hardened:** `electron-builder.yml`+`release/win-unpacked/Nexus v3.exe` existe, pero sin single-source version, sin offline models bundle check, sin updater.

---

### Fase 0 — Baseline medible (pre-requisito)

#### Task 0.1: Congelar métrica baseline y reproducir gap

**Objective:** Dejar traza de estado actual para medir progreso.

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/.hermes/plans/2026-09-06-nexus-v3-hermes-parity.md` (este plan)

**Step 1: Ejecutar batería baseline**

Run:
```bash
python D:/ias/proyectos/nexus-v3/run_tests.py
# Expected: 111/111 PASS (8 suites)

python -c "import urllib.request,json; print(json.loads(urllib.request.urlopen('http://127.0.0.1:8765/api/cloud/free-models?verify=true&force=true',timeout=50).read().decode())['free_models'][:3])"
# Expected: 6 models (solo deepseek fuera)

python -c "import urllib.request,json; d=json.loads(urllib.request.urlopen('http://127.0.0.1:8765/api/providers',timeout=10).read().decode()); print([m['id'] for m in d['models'] if m['provider']=='cloud'])"
# Actual: 2-3 (gap registered vs verified)
```

**Step 2: Guardar salida en `tmp/baseline-2026-09-06.log` y commitear**

---

### Fase 1 — Backbone E2E real (hace que `/api/chat` sea fiable)

#### Task 1.1: Fix `registered` vs `verified` — Cloud debe registrar pool descubierto

**Objective:** Que `/api/providers` exponga los 6 verified del discovery, no solo `config.yaml:models`.

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/src/api/server.py:159-191` (`_build_router_from_config`)
- Modify: `D:/ias/proyectos/nexus-v3/src/api/server.py:1083-1185` (router en `/api/chat`)
- Modify: `D:/ias/proyectos/nexus-v3/src/core/provider_router.py` (`refresh_cloud_free_models` → `register_verified_into_router`)
- Test: `D:/ias/proyectos/nexus-v3/tests/test_cloud_register.py` (nuevo)

**Step 1: Test que falla**
```python
# tests/test_cloud_register.py
import asyncio
from src.core.provider_router import ProviderRouter
from src.providers.cloud_provider import CloudProvider

async def test_register_verified_visible():
    router = ProviderRouter(vram_budget_gb=12, fallback_order=["cloud"])
    cp = CloudProvider(base_url="https://opencode.ai/zen", api_key="fake")
    router.register_provider("cloud", cp)
    # simula cache verified 6
    cp._free_cache = ["a-free","b-free","c-free","d-free","e-free","f-free"]
    for m in cp._free_cache:
        from src.core.provider_router import ModelRoute
        router.register_model(ModelRoute(id=m, provider_name="cloud"))
    assert len([m for m in router.list_models() if m.provider_name=="cloud"]) == 6
```

Run: `pytest tests/test_cloud_register.py -v` → FAIL (registra 0).

**Step 2: Implementar** — en `_build_router_from_config` y en `/api/chat`, tras crear `CloudProvider`, si `auto_discover:true` leer `data/cloud_free_cache.json` o `router._cloud_verified` y `register_model` para cada id `*-free` no duplicado. Factorizar helper `register_cloud_verified(router, verified: list[str])`.

**Step 3:** `pytest tests/test_cloud_register.py -v` → PASS. Verificar `curl /api/providers | jq '.models[] | select(.provider=="cloud") | .id'` → 6.

**Step 4: Commit**
```bash
git add src/api/server.py src/core/provider_router.py tests/test_cloud_register.py
git commit -m "fix(cloud): register verified free pool into router (6 not 2)"
```

#### Task 1.2: Deduplicar construcción de Router — extraer factory

**Objective:** Eliminar duplicación `_build_router_from_config` (línea 159) vs inline en `/api/chat` (1126+).

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/src/core/provider_router.py` — nuevo `build_router_from_config(cfg: dict, project_root: Path) -> ProviderRouter`
- Modify: `D:/ias/proyectos/nexus-v3/src/api/server.py` — ambos call sites usan factory; `/api/chat` además llama `register_cloud_verified`
- Test: `tests/test_provider_router.py` existente debe seguir 12/12

**Step 1:** Extraer función, mover `auto_vram_budget`, `register_provider` y `register_model` loops allí. Firma: `def build_router_from_config(cfg: dict, project_root: Path | None = None) -> ProviderRouter`.

**Step 2:** `python run_tests.py` → 111/111 + nuevo test 1.2.

**Step 3: Commit** `refactor(router): single factory for ProviderRouter`.

#### Task 1.3: E2E chat real con Zen — test `write→read→exec` con tool-calling

**Objective:** Probar que un modelo Zen free resuelve tarea multi-tool (no mock local).

**Files:**
- Create: `D:/ias/proyectos/nexus-v3/tests/test_e2e_zen_tools.py`
- Modify: `D:/ias/proyectos/nexus-v3/src/api/server.py:1084` — permitir `model` override verificado

**Step 1: Test (skipea si Zen off)**
```python
@pytest.mark.e2e
async def test_e2e_zen_write_read():
    # POST /api/chat {message:"Crea workspace/e2e_zen.txt con 'hola zen' y léelo", model:"nemotron-3.5-lightning-free"}
    # Asserts: LoopResult.success, trajectory contiene write_file + read_file, reply menciona hola zen
    # Usa httpx AsyncClient contra http://127.0.0.1:8765
```

Run: `pytest tests/test_e2e_zen_tools.py -v -m e2e` → si Zen 200 PASS, si 429/503 debe ir a Task 2.1 retry.

**Step 2: Ajustar AgentLoop system_prompt para forzar tool use cuando task menciona archivo.**

**Step 3: Commit** `test(e2e): zen write→read→exec proof`.

#### Task 1.4: Health coherente en `/api/providers` (no ocultar verified)

**Objective:** `providers.cloud.models` y `providers.cloud.registered` reflejen verified pool; `status` derivado de verify cache no de probe efímero.

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/src/api/server.py:252-327` (`/api/providers`)

**Step 1:** Inyectar en respuesta `free_verified: [...]` y `free_verified_count`.

**Step 2:** `curl /api/providers | jq .` verificado.

---

### Fase 2 — Resiliencia intra-cloud (la lógica que faltaba)

#### Task 2.1: Retry intra-cloud — si el modelo elegido falla, probar siguiente verified free

**Objective:** Si `ling 503` en medio del `AgentLoop.run`, no abortar; reintentar con `mimo`/`muse` del pool verified antes de fallback a llamacpp/ollama.

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/src/core/provider_router.py` — nuevo `resolve_verified_fallback_order(hint) -> list[ModelRoute]` ordenado thompson+vram pero filtrado a verified
- Modify: `D:/ias/proyectos/nexus-v3/src/core/agent_loop.py:226-400` — envolver `provider.chat(...)` en loop sobre `fallback_verified`
- Test: `D:/ias/proyectos/nexus-v3/tests/test_cloud_fallback.py`

**Step 1: Test que falla**
```python
async def test_fallback_on_503(monkeypatch):
    # provider.chat mock: primera 503, segunda 200
    # AgentLoop debe reintentar con siguiente verified y succeed
```

**Step 2: Implementar** — `AgentLoop._call_with_fallback(messages, verified_order)` con jitter 200ms, máx reintentos = min(3, len(verified)).

**Step 3: Commit** `feat(cloud): intra-cloud fallback across verified free pool`.

#### Task 2.2: Cache verify con TTL dual ya correcto — agregar métrica observable

**Objective:** Exponer `verify cache age + health per model` en `/api/cloud/free-models`.

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/src/providers/cloud_provider.py` — `probe_free_models_health` ya distingue permanent/transient; agregar `last_verify_ts` y `per_model_health` dict
- Modify: `D:/ias/proyectos/nexus-v3/src/api/server.py:329-361` — endpoint devuelve `health_detail: {model: {ok, code, latency_ms, age_s, permanent}}`

**Step 1-2:** Implementar y `curl /api/cloud/free-models?verify=false` incluye `health_detail`.

---

### Fase 3 — MCP + Skills/Gemas (paridad Hermes)

#### Task 3.1: MCP Bridge mínimo viable

**Objective:** `src/mcp/bridge.py` conecta a 1 servidor MCP stdio y registra sus tools en `ToolRegistry` como categoría `mcp`.

**Files:**
- Create: `D:/ias/proyectos/nexus-v3/src/mcp/bridge.py`
- Modify: `D:/ias/proyectos/nexus-v3/src/mcp/__init__.py`
- Modify: `D:/ias/proyectos/nexus-v3/src/api/server.py:1183` — tras `ToolRegistry()` cargar `MCPBridge.load(registry, config.mcp.servers)`
- Test: `tests/test_mcp_bridge.py`

**Spec:** Copiar patrón Hermes `src/bridges/mcp_bridge_server.py` (stdio transport, `list_tools`→`register_mcp_tool`). Config en `config.yaml: mcp.servers: [{name, command, args}]`.

#### Task 3.2: Skill/Gema bundle loader

**Objective:** Cargar bundles `skills/<name>/SKILL.md + tools.json` en `ToolRegistry` categoría `skill`/`gema`.

**Files:**
- Create: `D:/ias/proyectos/nexus-v3/src/core/skill_loader.py`
- Modify: `D:/ias/proyectos/nexus-v3/src/core/tool_registry.py` — ya tiene `load_skill_bundle`/`load_mcp_tools`; cablear directorio scan
- Test: `tests/test_skill_loader.py`

#### Task 3.3: Pasar `mcp`+`skill` tools al LLM en `AgentLoop`

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/src/core/agent_loop.py:226` — `tool_registry.to_openai_tools()` ya incluye todas categorías; verificar `include_mcp=true` y test tool_choice

---

### Fase 4 — UI/Electron sin placeholders (auditoría devtools)

#### Task 4.1: Auditoría devtools — capturar estado actual

**Objective:** Abrir `http://127.0.0.1:8765` + `http://localhost:5174` con browser tool, screenshot, listar endpoints usados vs mock.

**Files:**
- Create: `D:/ias/proyectos/nexus-v3/docs/audit-ui-2026-09-06.md` (screenshots + tabla contratos)

**Checks:** `ChatPanel` usa `chatApi` real? `ModelSelector` lista `providersApi().models` (hoy 3, debe 6)? `FileEditor` usa `/api/fs/read|write`? `TerminalPanel` usa `/api/exec`? `GraphView` usa `/api/graph`? `SettingsDialog` usa `/api/providers/config`?

#### Task 4.2: ChatPanel streaming + estados reales

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/ui/src/components/chat/ChatPanel.tsx`
- Modify: `D:/ias/proyectos/nexus-v3/ui/src/lib/api.ts` — agregar `chatStreamApi` si backend soporta SSE, o polling `session/messages`

**Fix:** Eliminar mensajes simulados; mostrar `mode`, `error`, `session_id` reales; spinner por `LoopResult` iterations; render tool calls en timeline.

#### Task 4.3: ModelSelector — mostrar verified pool + health

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/ui/src/components/ModelSelector.tsx`
- Modify: `D:/ias/proyectos/nexus-v3/ui/src/store/app-store.ts`

Mostrar badge `verified 6 / registered 6`, tooltip per-model `200/429/500` con age.

#### Task 4.4: FileEditor + TerminalPanel contratos reales

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/ui/src/components/editor/FileEditor.tsx` — `fsRead`/`fsWrite` con Scope error surface
- Modify: `D:/ias/proyectos/nexus-v3/ui/src/components/terminal/TerminalPanel.tsx` — `execCmd` con cwd, timeout, truncado 20k, exit_code badge

#### Task 4.5: Eliminar placeholders topbar

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/ui/src/App.tsx` — quitar bell/notifications dummy, search conecta a `session search` (`/api/sessions/{id}/search` + FTS), o esconder si no implementado

#### Task 4.6: Electron hardening check

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/electron/main.ts` — verificar `contextIsolation:true`, `sandbox:true`, `preload` vía `contextBridge`, single-instance, kill pyProc en `before-quit`, health polling
- Test: `npm run lint:electron` + manual `npm run dev` + `npm start` (prod SPA)

---

### Fase 5 — Memoria/Brain/RAG bajo carga

#### Task 5.1: Ingesta RAG verificada

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/src/core/rag.py`, `src/core/embeddings.py`, `src/api/server.py:750-826`
- Test: `tests/test_rag_ingest.py` — `POST /api/rag/index-graph` → `GET /api/rag/search?q=...` retorna hits

#### Task 5.2: Session FTS + compaction bajo carga

**Files:**
- Test: `tests/test_session_load.py` — crear 200 msgs, forzar `compact_threshold 0.75`, verificar spill en `data/spill/` y FTS search

#### Task 5.3: Brain link 3-tier (working/episodic/semantic)

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/src/brain/link.py` + `config.yaml:brain` — verificar `NEXUS_BRAIN` override, TTL por tier, `get_brain_info` en `/api/health`

---

### Fase 6 — Distribución independiente

#### Task 6.1: Version single-source + `npm run dist` smoke

**Files:**
- Modify: `D:/ias/proyectos/nexus-v3/package.json:version`, `electron-builder.yml`, `src/api/server.py: app.version`
- Run: `npm run build && npx electron-builder --dir` → `release/win-unpacked/Nexus v3.exe` lanza y `/api/health` responde

#### Task 6.2: Offline bundle check — `models/` + `vendor/llama.cpp` presentes

**Files:**
- Create: `D:/ias/proyectos/nexus-v3/scripts/check_offline_bundle.py` — lista `models/*.gguf` + `vendor/llama.cpp/build/bin/llama-server(.exe)` y falla si falta residente `nexus-director-v6`

#### Task 6.3: Hardening checklist (BLUEPRINT §12 audit)

**Files:**
- Create: `D:/ias/proyectos/nexus-v3/docs/hardening-checklist.md` — 12-layer audit (wrapper-regression, memory-pollution, hidden-repair-loops, Scope bypass, etc.)

---

## Orden de ejecución recomendado

1. 0.1 → 1.1 → 1.2 (desbloquean todo)
2. 1.3 (E2E proof) + 2.1 (fallback) en paralelo — si E2E falla, 2.1 lo arregla
3. 4.1 auditoría (puede ir en paralelo con 2.x)
4. 3.1-3.3 MCP/skills
5. 4.2-4.6 UI fixes
6. 5.1-5.3 memoria
7. 6.1-6.3 distribución

Cada task con TDD + commit. Tras cada fase: `python run_tests.py` + `curl /api/cloud/free-models?verify=true&force=true` + `curl /api/providers | jq`.

## Criterio de done (paridad Hermes/OpenCode)

- [ ] E2E `write→read→exec` con Zen free pasa sin intervención (intra-cloud fallback cubre 503/429)
- [ ] `/api/providers` muestra 6 verified, ModelSelector los lista, health_detail observable
- [ ] MCP bridge carga 1 server externo y sus tools aparecen en `ToolRegistry` y en el prompt del AgentLoop
- [ ] UI sin mocks: chat, editor, terminal, graph, providers config todos contra backend real (devtools audit doc con screenshots)
- [ ] `npm run dev` y `npm start` funcionan; `release/win-unpacked/Nexus v3.exe` arranca offline con models vendored
- [ ] RAG search + FTS search + compaction spill verificados bajo carga
- [ ] `run_tests.py` ≥ 120 tests (111 actuales + ≥9 nuevos por fases) todos PASS


### Fase 7 — Recuperar exportación incompleta SuperNEXUS v2 + Patrones Autopsia (tú lo pediste)

> Esta fase existe porque `D:\ias\autopsia` (73 repos, 187 patrones) y `D:\ias\proyectos\supernexus-v2` (229 módulos core, 3 779 LOC → 73 605 LOC) no se exportaron completos a Nexus v3. Nexus v3 tiene 18 módulos core + 0 tools + 0 bridges. Faltan **211 módulos** y **7 tools + 13 bridges**. No se reimplementa desde cero: se **porta verbatim lo probado** y se adapta imports/paths.

**Inventario auditado 2026-09-06 (conteos reales):**

| Origen | Nexus v3 hoy | SuperNEXUS v2 | Gap |
|---|---|---|---|
| `src/core/*.py` | 18 files, 3 779 LOC | 229 files, 73 605 LOC | **211 sin exportar** |
| `src/tools/` | no existe | 8 files (`builtin`, `cdp_browser`, `code_search`, `codegraph_tool`, `persistent_shell`, `skill_gateway`, `vision_gem_v2`, `web_fetch`) | **todo** |
| `src/bridges/` | no existe | 13 files (`mcp_bridge_server` 3 181 LOC, `mcp_autodiscovery`, `nexus_bridge`, `tailscale_bridge`, `ssh_bridge`, etc.) | **todo** |
| Autopsia 73 repos | no aplicado | 187 patrones en `_SYSTEM_ABSORPTIONS.md` + `_FINAL_ABSORPTION_REPORT.md` | aplicar críticos 🔴 |

**Taxonomía de los 211 faltantes (por qué importan para paridad Hermes/OpenCode):**

1. **Harness/Orchestration** — `director.py` (1 830 LOC), `conductor.py`, `dag_coordinator.py`, `agent_runner.py`, `agent_teams.py`, `hive_workflow_engine.py` (1 451 LOC), `background_workers.py` (1 211 LOC), `gema_host/worker` → sin esto no hay multi-agente ni workers persistentes.
2. **Seguridad** — `ai_defence.py` (935 LOC), `capability_security.py`, `capability_discovery.py`, `auth_vault.py`, `credential_manager.py`, `approval_gate.py` → sin esto toda gema puede todo.
3. **Memoria** — `hierarchical_memory.py`, `hybrid_memory.py`, `memory_consolidator.py` (1 194 LOC), `fts5_search.py`, `knowledge_vault.py`, `dream_consolidation.py`, `context_compactor.py`, `prompt_compressor.py` → Nexus solo tiene `SessionLog` plano; autopsia exige 6 tipos + FTS5 default.
4. **MCP/Bridges** — `mcp_client_bridge.py`, `mcp_hub.py`, `mcp_connection_manager.py`, `mcp_bridge_server.py` (38 tools), `mcp_autodiscovery.py` → sin esto `src/mcp/__init__.py` vacío.
5. **Tools** — `ai_tools.py` (2 886 LOC), `hermes_tools.py`, `builtin.WorkspaceTools` (drive-root fallback), `persistent_shell.py`, `code_search.py`, `codegraph_tool.py`, `vision_gem_v2.py`, `web_fetch.py` → Nexus solo 4 tools mock.
6. **Comunicación** — `event_bus.py`, `message_bus.py`, `peer_chat.py`, `a2a_server.py`, `acp_protocol.py`, `remote_node_bridge.py` → sin esto no hay PC1↔PC2 bridge ni A2A `/.well-known/agent.json`.
7. **Observabilidad/Resiliencia** — `circuit_breaker.py`, `loop_guard.py`, `loop_detector.py`, `doctor.py`, `graceful_degradation.py`, `checkpoint.py` + Checkpoint Contract.
8. **Training/Research** — `deep_research.py`, `nexus_trainer.py`, `muon_trainer.py`, `self_improving.py`, `model_autopsy.py`.

**Patrones Autopsia 🔴 absorbidos aquí:** Checkpoint Contract + SwarmBrief (hermes-workspace), Capability-based security (openfang), 6 tipos memoria + Topic upserts + FTS5 default (openakita/engram), MCP fallback + autodiscovery (odysseus/aden-hive), DMN + Notification scoring (lethe), Thompson sampling router (ruflo), OFP peer HMAC-SHA256 (openfang-wire).

#### Task 7.1: Inventario priorizado `docs/EXPORT_GAP.md`
**Files:** Create `docs/EXPORT_GAP.md` — tabla 211 files con `P0/P1/P2`, fuente autopsia, LOC, dependencias, orden topológico. Modify este plan.
**Steps:** Generar desde `ls -1 supernexus-v2/src/core` vs `nexus-v3/src/core` + `src/tools` + `src/bridges` + `_SYSTEM_ABSORPTIONS.md` 🔴/🟡. Clasificar P0=aporta a Fases 1-3 o seguridad, P1=memoria/orquestación, P2=descartar. Orden topológico ej: `provider_base` → `auth_vault` → `ai_defence` → `capability_*` → `mcp_*` → `builtin` → `director` slices. Failing test: `EXPORT_GAP.md` existe y lista 211 entries categorizados. Commit.

#### Task 7.2: Port P0 — `src/tools/` + `src/bridges/mcp_bridge_server` verbatim
**Files:** Copy verbatim (adaptar solo imports): `tools/builtin.py` → `nexus-v3/src/tools/builtin.py`, `persistent_shell.py`, `code_search.py`, `web_fetch.py`, `codegraph_tool.py` + `bridges/mcp_bridge_server.py` (38 tools) → `nexus-v3/src/mcp/bridge_server.py` + `mcp_autodiscovery.py` + `mcp_connection_manager.py` + `mcp_hub.py` + `ai_defence`/`capability_*` slices. Modify `tool_registry.py` (Scope-guarded) y `server.py` (`GET /api/mcp/servers`, `POST /api/mcp/call`).
**Failing test:** `test_builtin_write_read_roundtrip` + `test_mcp_bridge_lists_38_tools` PASS tras port; `curl /api/mcp/servers` lista 38 tools.

#### Task 7.3: Port P0 — `ai_defence` + `capability_*` + `approval_gate` + `checkpoint`
**Files:** Copy `ai_defence.py` (935 LOC), `capability_security.py`, `capability_discovery.py`, `approval_gate.py`, `checkpoint.py` → `nexus-v3/src/core/` + envolver `agent_loop.py` con `CapabilitySet` + `Checkpoint` emit `STATE/FILES_CHANGED/COMMANDS_RUN/RESULT/BLOCKER/NEXT_ACTION`.
**Failing test:** `exec` sin capability bloqueado; `agent_loop` emite checkpoint estructurado.

#### Task 7.4: Port P1 — Memoria jerárquica + FTS5 default
**Files:** Copy/adapt `hierarchical_memory.py`, `hybrid_memory.py`, `fts5_search.py`, `context_compactor.py` (679 LOC) → `nexus-v3/src/core/`+`src/brain/`; integrar `compaction.py` L1-L4; upserts por `topic_key` + soft delete dedupe.
**Failing test:** mismo `topic_key` 2× → update (revision_count++) no duplicar; FTS5 sin ChromaDB funciona; 100 obs / 50 queries bajo carga.

#### Task 7.5: Port P1 — Orchestration (`director` slices + `conductor` + `dag_coordinator` + `background_workers`)
**Files:** Copy slices `director.py` (1 830 LOC) → `nexus-v3/src/core/director.py` delegando a `conductor.py`+`dag_coordinator.py`, `background_workers.py` (1 211 LOC), `gema_host/worker`; `server.py` `POST /api/agent/run` acepta `SwarmBrief` + streaming checkpoints.
**Failing test:** `POST /api/agent/run` multi-file crea DAG 3 nodos y stream `IN_PROGRESS → DONE` por nodo.

#### Task 7.6: Port P1 — Comunicación (`event_bus` + `a2a_server` + `remote_node_bridge`)
**Files:** Copy `event_bus.py`, `message_bus.py`, `a2a_server.py`, `remote_node_bridge.py`, `tailscale_bridge.py` → `nexus-v3/src/core/`+`src/bridges/`; `server.py` `GET /.well-known/agent.json`, `POST /api/remote/exec`.
**Failing test:** `GET /.well-known/agent.json` → AgentCard válido; `event_bus` 40+ tipos (aden-hive).

**Criterio cierre Fase 7:** `docs/EXPORT_GAP.md` con 211 entries; `src/tools/` 5 files + `src/mcp/bridge_server.py` 38 tools operativos; `ai_defence` bloquea tool sin capability; checkpoint estructurado emitido; FTS5 sin ChromaDB funciona; `/.well-known/agent.json` responde. Sin esto, Fases 1-6 quedan en mock.

---

## Orden de ejecución actualizado

1. 0.1 → 1.1 → 1.2 (desbloquean todo)
2. 7.1 inventario (desbloquea 7.2-7.6; puede ir en paralelo con 1.3/2.1)
3. 1.3 + 2.1 en paralelo — E2E proof + fallback intra-cloud
4. 7.2 + 7.3 P0 tools/MCP+seguridad (sin esto no hay Fase 3 real)
5. 4.1 auditoría devtools + 3.1-3.3 MCP/skills en paralelo
6. 4.2-4.6 UI fixes + 7.4 memoria jerárquica + 5.1-5.3 memoria/RAG
7. 7.5-7.6 orchestration/comunicación + 6.1-6.3 distribución
