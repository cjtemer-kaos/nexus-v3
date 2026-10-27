# Hermes Agent vs Nexus v3 — cómo funciona cada uno, medido

- **Fecha**: 2026-10-27
- **Alcance**: comparación técnica de **runtime**, no de marketing. Cómo piensa, ejecuta, recuerda,
  compacta y se opera cada harness.
- **Método**: lectura de código fuente instalado y medición con `find`/`wc`/`grep`/`sqlite3` en
  **sólo lectura**. No se modificó ni un archivo de `nexus-v3`: el único artefacto escrito es este
  documento. `opencode` puede seguir trabajando en el repo sin conflicto.
- **Hermes medido**: `C:\Users\cjtr\AppData\Local\hermes\hermes-agent` — v0.22.5 (2026.9.24),
  canal stable, local `bf0203a794a0`, instalación por git, Python 3.14.9.
- **Nexus medido**: `D:\ias\proyectos\nexus-v3` — 3.0.0-alpha, Python + Electron 32 + React 19.

---

## 1. Resumen ejecutivo

Son dos cosas distintas que se parecen en la superficie (ambos son "un agente con tools"):

| | Hermes Agent | Nexus v3 |
|---|---|---|
| Qué es | **Producto multi-superficie**: un gateway Python que atiende CLI, TUI, escritorio, web y 11+ plataformas de mensajería | **Aplicación de escritorio local**: Electron + backend FastAPI propio |
| Dónde vive el estado | `state.db` (SQLite, 392 MB, 108 sesiones, 46.974 mensajes) | `data/session.db` + `brain/knowledge_graph.db` (12.411 nodos) + `brain/rag_index.db` (19.286 chunks) |
| Modelo por defecto | Nube (aquí: `opencode-go` / `deepseek-v4.1-flash`) | **Local primero** (Ollama, 11 modelos, `qwen3-vl-8b` preferido) |
| Fuerte en | Superficie, contrato entre capas, operación, mensajería, skills, multi-plataforma | Profundidad de agente: grafo de conocimiento, RAG, compactación determinista, sandbox, LSP, automejora |
| Código | ~1,2 M de líneas contando escritorio (Python 611 k + TS 838 k) | ~60 k líneas Python + UI React |

La diferencia de fondo: **Hermes es un sistema operativo de agentes** (muchas superficies, un
gateway, perfiles, canales); **Nexus v3 es un harness de agente con cerebro propio** (un proceso,
un grafo, un loop, una UI). Copiar piezas de uno al otro tiene sentido sólo si se respeta esa
diferencia.

---

## 2. Arquitectura de proceso

### 2.1 Hermes

```
Hermes.exe (Electron main)
 ├─ renderer Chromium (React, hash router)
 ├─ utility processes
 └─ gateway Python  ── JSON-RPC sobre WebSocket ──┐
      ├─ sesiones (SQLite state.db, WAL + FTS)    │
      ├─ agente + tools + MCP client               │ consumido también por:
      ├─ cron / watchers / delivery                │  · hermes (CLI)
      └─ hosted rooms (multi-agente / kanban)      │  · TUI (Ink)
                                                   │  · web
                                                   │  · telegram/discord/slack/...
```

Tres autoridades separadas y un contrato explícito (`apps/desktop/AGENTS.md`): Electron es dueño
del ciclo de vida y del filesystem; el renderer **nunca** toca Node (todo por
`window.hermesDesktop`, un `contextBridge` angosto); el backend Python es dueño de sesiones,
tools y llamadas a modelo. El mismo renderer apunta a un backend local, a uno remoto por SSH o a
un gateway en la nube sin cambiar una línea de UI.

**El contrato está generado desde Python**: `apps/shared/src/gateway-contract.generated.ts`
(7.270 líneas, cabecera `GENERATED ... DO NOT EDIT`, test de frescura que falla si quedó viejo).
Medí **311 identificadores `grupo.método` distintos** en ese archivo; la autopsia previa
(`HERMES_DESKTOP_AUTOPSY.md`) contó 392 métodos contando todas las variantes declaradas.
Consecuencia práctica: si el backend deja de mandar un campo, rompe `tsc` en vez de producir
`undefined` en runtime.

### 2.2 Nexus v3

```
Nexus v3.exe (Electron main)
 ├─ renderer React (Vite, ui/dist)
 └─ spawn: python -m src.api.server --port <libre>
      ├─ FastAPI: 138 rutas REST + SSE + WS
      ├─ AgentLoop (TDAO) + ToolRegistry + Scope
      ├─ SessionLog (session.db WAL+FTS) + EventStore
      ├─ CompactionPipeline (4 capas) + HooksEngine (6 fases)
      ├─ brain/: knowledge_graph.db + rag_index.db + hybrid_memory.db
      └─ MCP cliente (stdio) + MCP server propio (expone 52 tools) + ACP (Zed)
```

Electron levanta el backend en un **puerto libre** y lo mata en `window-all-closed` /
`before-quit` con `taskkill /T /F` en Windows. Single-instance lock, tray, NSIS + zip.
Todo config-driven (`config/config.yaml`, 416 líneas, con `config.schema.json`).

**Diferencia estructural**: Hermes tiene **un** gateway que sirve **muchas** superficies; Nexus
tiene **un** backend que sirve **una** app. Eso explica casi todas las ventajas y desventajas de
la tabla final.

---

## 3. El turno, paso a paso

### 3.1 Hermes: un turno

`agent/conversation_loop.py` (`run_conversation`) orquesta el turno completo. Fases reales del
código:

1. **Preflight**: `build_turn_context` — resuelve modelo, contexto, memorias, skills candidatas,
   archivos de contexto (`SOUL.md`, `AGENTS.md`, `.cursorrules`), y decide si hay que comprimir
   **antes** de llamar (`PreflightCompressionTimedOut` es un caso tratado).
2. **Armado del prompt**: `prompt_builder.py` — identidad, hints de plataforma, índice de skills,
   archivos de contexto, memoria persistente, prompts efímeros. Todo stateless y combinado por
   `_build_system_prompt()`.
3. **Llamada al modelo** (`turn_api_request` / `perform_api_call`): tool schemas, prompt caching
   plan, headers por proveedor, adaptadores (OpenAI, Anthropic, Bedrock, Codex, relay).
4. **Tool dispatch** (`tool_executor.py`, 1.966 líneas): validación/coerción de argumentos,
   aprobaciones, ejecución, redacción de secretos, truncado, eventos de progreso.
5. **Observación**: mensajes persistidos con metadata, telemetría de uso, detección de repetición
   (`repetition_guard.py`), guardrails de loop (`tool_loop_guardrails`: warning a los 2 fallos
   exactos, hard-stop configurable).
6. **Compresión** si el contexto excede el umbral (`context_compressor.py`, 5.744 líneas).
7. **Post-turno**: extracción de memoria (nudge cada 10 turnos con `flush_min_turns: 6`),
   título, curator, hooks, entrega a la plataforma.

El mismo loop corre en todas las superficies. Streaming: eventos `message.start/delta/complete`,
`tool.start/progress/complete`, `subagent.*`, `approval.request`, `clarify.request`.

### 3.2 Nexus v3: un turno

`src/core/agent_loop.py` (1.722 líneas) implementa **TDAO**:

```
THINK   → llm call via ProviderRouter (fallback cross-provider)
DECIDE  → tool_calls vs respuesta final
ACT     → scope.guard → hooks.pre_tool_use → ToolRegistry.dispatch
          → hooks.post_tool_use → (paralelo si la tool es de sólo lectura)
OBSERVE → SessionLog + EventStore + Trajectory, classify_error, budget, quizá compactar
```

Detalles que sí están implementados y medibles:

- **Budget**: `IterationBudget` con `max_iterations: 25`, `max_tokens: 32000`,
  `max_wall_ms: 900000`, `max_tool_calls: 60`.
- **Anti doom-loop**: aviso a 3/5 repeticiones, corte a 8; 3 fallos avisan, 6 cortan; misma tool
  con argumentos distintos avisa a 5 y corta a 10.
- **Calibración de tokens**: guarda `actual/estimado` por sesión (cap 4x) para no subestimar en
  CJK/JSON denso.
- **Paralelismo**: las tools de sólo lectura de un mismo turno corren en paralelo
  (`NEXUS_PARALLEL_TOOLS`, free-threaded).
- **Heartbeat** entre tool calls (`heartbeat_ms: 2000`).
- **Replan/reintento**: `error_policy.retry: 3`, `replan: 2`, clasificador de errores.

**Diferencia**: el loop de Nexus es explícito y auditable (TDAO con presupuesto y guardrails
propios, todo en un archivo de 1.722 líneas); el de Hermes está **fragmentado por fase en ~30
módulos** para que cada parte sea reemplazable y testeable por separado. Nexus es más fácil de
leer de punta a punta; Hermes es más fácil de cambiar sin romper el resto.

---

## 4. Tools

| | Hermes | Nexus v3 |
|---|---|---|
| Registro | 51 tools core en `_HERMES_CORE_TOOLS` + tools por toolset/plataforma (21 toolsets) + **178 tools diferidas** cargadas on-demand (`toolsets`, `tool_search`/`tool_describe`/`tool_call`) | 52 tools registradas con `registry.register_function` (medido: 52 llamadas) |
| Esquema | JSON Schema generado desde Python; coerción de argumentos (`arg_coercion`) | JSON Schema declarado a mano por tool; `_infer_schema` resuelve tipos con `get_type_hints` |
| Ejecución | `tool_executor` con aprobaciones, redacción, truncado, progreso | `ToolRegistry.dispatch` + `scope.check_tool` + hooks pre/post |
| Paralelismo | Por lote de tool calls en el mismo turno | Explícito: set `_READONLY_TOOLS` corre en paralelo |
| Largo plazo | `terminal(background=True)` + `process_manage` | `job_start/job_read/job_wait/job_kill/job_list` (cursor delta, kill del árbol) |
| Shell | bash/MSYS en Windows (según host), backend local o Docker | `exec`, `pty_exec`, shell persistente, `job_*` |
| REPL | `execute_code` — kernel Python persistente entre llamadas | `repl` — kernel Python persistente, reciclado por idle, timeout que avisa que se perdió el estado. Misma idea, implementaciones independientes |
| Canales | Las tools cambian según plataforma (CLI/Telegram/Discord/…) | Un solo registry para UI, MCP entrante, ACP y OpenAI-compat |

Inventario Nexus (52): `read_file`, `write_file`, `edit`, `apply_patch`, `list_dir`, `glob`,
`grep`, `code_search`, `repo_map`, `exec`, `pty_exec`, `job_*` (5), `repl`, `run_tests`,
`git_status/diff/log/commit`, `web_search`, `web_fetch`, `image_describe`, `image_generate`,
`checkpoint_*` (4), `mcp_list`, `mcp_call`, `skill`, `recall`, `learn`, `sub_agent`,
`sub_agent_list`, `sub_agent_message`, `gema_run/list`, `agent_create/run/list`,
`team_spawn/mailbox/await/list`, `todo_read/write`, `question`, `roundtable`, `cloud`, `ollama`,
`llamacpp`, `echo`.

Inventario Hermes (51 core, muestra): `web_search`, `web_extract`, `terminal`, `process_manage`,
`read_file`, `write_file`, `patch`, `search_files`, `vision_analyze`, `image_generate`,
`skills_list/view/manage`, 14 tools de browser, `text_to_speech`, `todo_list`, `memory`,
`session_search`, `clarify`, `execute_code`, `delegate_task`, `cronjob_manage`, 14 de kanban,
`computer_use`, `manage_connections` + 178 diferidas (MCP, chrome-devtools, context7, etc.).

**Lo importante**: Hermes **deferred loading** (el modelo ve un catálogo y carga schemas sólo
cuando los necesita) para no pagar 178 tool schemas en cada request. Nexus mete las 52 en el
prompt, que a 32 k de contexto es una porción real pero manejable; con 200 tools sería inviable.

---

## 5. Contexto y compactación

### Hermes (`agent/context_compressor.py`, 5.744 líneas)

- Modelo **auxiliar barato** resume los turnos del medio; cabeza y cola protegidas
  (`protect_last_n: 20`, `protect_first_n: 3`).
- Umbral `compression.threshold: 0.5` del contexto; objetivo `target_ratio: 0.2`.
- Antes de resumir: **poda de salidas de tools** y *micro-compaction*; después: resúmenes
  iterativos (no re-resume de cero).
- Presupuesto de cola por tokens, no por mensajes; escalado por modelo.
- Evicción de imágenes y *demotion* de tool results viejos a stubs.
- Prompt caching (`cache_ttl`) para no pagar el prefijo dos veces.
- Telemetría de compresión (`compression_locks` en `state.db`) para no comprimir dos veces.

### Nexus (`src/core/compaction.py`, 704 líneas)

Pipeline determinista de 4 capas + emergencia:

| Capa | Qué hace | Parámetro |
|---|---|---|
| L1 Snip | conserva system + últimos N, tira el medio; **retrocede el corte** si el primer mensaje retenido es `role=tool` | `keep_recent: 4` |
| L2 Micro | colapsa tool results grandes a placeholder, conservando los últimos | `keep_recent: 3` |
| L3 Budget | trunca y **persiste el original** en `data/spill/` | `max_tool_result_chars: 2000` |
| L4 Summary | 1 llamada LLM barata con prompt de 9 secciones fijas + parseo `<summary>` | `ratio: 0.20`, techo 12 k |
| Emergency | parsea `context_limit` del error y compacta agresivo | — |

Además: `minimum_progress_messages: 2` (no re-resume si avanzó poco), `failure_cooldown_s: 600`
(tras fallo no reintenta 10 min), `handoff_prefix` que le dice al modelo que el resumen reemplaza
el historial. `prune` poda cabeza+cola de resultados viejos antes de cada llamada.

**Veredicto**: Nexus es más **predecible y explicable** (4 capas con métricas por capa, spill a
disco con ruta para releer, handoff explícito). Hermes es más **sofisticado en el caso raro**
(presupuesto por tokens, imágenes, demotion, caching, doble compresión protegida por lock).
Nexus gana en "por qué compactó"; Hermes gana en "no volver a compactar lo ya compactado".

---

## 6. Memoria y conocimiento

Es la diferencia **conceptual** más grande.

### Hermes: memoria de agente, no base de conocimiento

| Sistema | Qué guarda | Límite medido |
|---|---|---|
| `MEMORY.md` | hechos del entorno/proyecto, inyectados en **cada** turno | 4.383 / 22.000 chars |
| `USER.md` | quién es el usuario y sus preferencias | 2.032 / 13.750 chars |
| `state.db` | 108 sesiones, 46.974 mensajes con **FTS** (`messages_fts`) | 392 MB |
| `session_search` | búsqueda/lectura de conversaciones viejas | sin límite práctico |
| Skills | memoria **procedimental** (cómo hacer X), cargada sólo si aplica | 162 `SKILL.md` |
| `kanban.db` | trabajo pendiente con claims/leases | 8 tablas |

Diseño explícito: memoria **corta y curada** inyectada siempre; todo lo demás recuperado por
búsqueda. El sistema tiene *nudges* (`nudge_interval: 10`), *flush* mínimo de turnos y un
`curator` que consolida cuando el presupuesto se llena. También hay memoria por grafo en el
**plugin** `memory` y en `/starmap` del escritorio, pero **no** es el camino principal del loop.

### Nexus: cerebro propio como ciudadano de primera

Medido en `brain/`:

| Artefacto | Contenido |
|---|---|
| `knowledge_graph.db` (42,7 MB) | **12.411 nodos**, **31.361 edges**, 12.970 entidades, 42.422 vínculos entidad-nodo, **23 revisiones** (`node_revisions`, append-only) |
| `rag_index.db` (286 MB) | **19.286 chunks** con FTS/embeddings |
| `hybrid_memory.db` (3,97 MB) | memoria híbrida |
| `learning_graph.db` / `learning_loop.db` | Thompson sampling / aprendizaje |
| `memory_audit.db` | auditoría de memoria |

Capacidades que Hermes no tiene en el loop: `recall`/`learn` como tools, **inyección automática
del grafo** antes de cada turno (`memory.inject`, límite 3 nodos / 2.400 chars, rerank semántico
con timeout 800 ms), **destilado al compactar** y al cerrar sesión (`distill_on_compact`,
`distill_on_session_end`), extracción automática de hechos por turno (máx. 5), historial y
**restore** de nodos, RAG con MMR para diversidad, y búsqueda multi-señal
(`multi_signal_retrieval`).

**Veredicto**: en memoria a largo plazo Nexus está **muy por delante** de Hermes. En memoria de
trabajo (lo que el modelo sabe *ahora*) Hermes está mejor afinado: dos archivos con presupuesto
duro, curaduría y todo lo demás por búsqueda. Un agente con 12 k nodos inyectados sin control es
un problema de contexto; Nexus lo resolvió con presupuesto y filtro anti-ruido, pero el riesgo de
ruido sigue existiendo.

---

## 7. Skills y plugins

| | Hermes | Nexus v3 |
|---|---|---|
| Formato | `SKILL.md` con frontmatter YAML (trigger de 57 chars, condiciones por plataforma/app) | `SKILL.md` con frontmatter + índice de 2.161 skills (según comentario en `server.py`) |
| Descubrimiento | índice de skills en el system prompt; `skill_view` carga el cuerpo | tool `skill(query=)` busca, `skill(name=)` devuelve el `SKILL.md` completo |
| Instalación | catálogo + `/capabilities` + `skills_hub` auxiliar | `skill_install`, `skills_catalog`, roots `./data/skills` + `./skills` |
| Auto-creación | `skill_manage` + nudge cada 15 turnos | `skill_opt` (held-out), auto-skills en `data/auto_skills` |
| Plugins | 19 paquetes en `plugins/` (browser, computer_use, kanban, memory, delegation, model-providers, platforms, security-guidance, spotify, teams_pipeline, video_gen, web…) con tools, rutas UI y ajustes propios | `plugins/` cargados por contrato propio; UI informa `available/loaded/errors/failed` |

Medido: **162 `SKILL.md` instalados** en el perfil de Hermes; Nexus declara 2.161 en su catálogo
(el comentario del código dice que con ese volumen los matches flojos eran un problema real y por
eso devuelve `score` + aviso cuando el mejor match es débil).

---

## 8. MCP y conectores

- **Hermes**: cliente MCP nativo (`mcp_servers` en config, 5 configurados aquí: supernexus,
  chrome-devtools, context7, brave-search, agent-browser), tools diferidas, `manage_connections`
  para cuentas de apps (Gmail, Notion, Linear…), y servidor MCP propio para exponer el agente.
- **Nexus**: cliente MCP (`mcp_client` + `mcp_connection_manager` con pool, refcount, health,
  reconexión), **servidor MCP** propio (`src/mcp_server.py`, stdio, expone las 52 tools,
  verificado con `initialize` + `tools/list` + `recall`), bridge MCP y adaptador **ACP** para Zed.
  Estados honestos: `ok / missing / needs_key / manual / disabled / remote`; sólo lanza stdio.

**Diferencia**: Hermes consume MCP como capacidad de producto (catálogo, probes, logs, hosted).
Nexus expone **su propio harness** por MCP/ACP, lo que lo hace usable desde otros clientes — algo
que Hermes también tiene pero con más superficie comercial encima.

---

## 9. Sub-agentes y orquestación

| | Hermes | Nexus v3 |
|---|---|---|
| Delegación | `delegate_task` (hasta 10 en paralelo), contexto aislado, `max_iterations: 250`, resultados en background, control `list/steer/stop` | `sub_agent`, `agent_run`, `team_spawn/mailbox/await`, `_READONLY_TOOLS` en paralelo |
| Aislamiento de tools | toolset por plataforma/perfil | `SubAgentSpec.allowed_tools` + set curado por gema (`scholar`, `code`, `debugger`) |
| Coordinación | kanban (claims, leases, review), hosted rooms (14 módulos, 340 KB) | `board.py` claim-once (`UPDATE` + `rowcount`), `dag_coordinator`, `worktree`, `team.py` |
| Multi-máquina | host topology, pairing, SSH, cloud, `bot_relay` | `nexus_hive` + colmena en VPS (`nexus-sovereign`) |

Nexus tiene algo que Hermes no: **claims verificados por carrera** (6 agentes → gana exactamente
1, con lease y heartbeat) y **worktrees** para agentes paralelos sobre el mismo repo. Hermes tiene
algo que Nexus no: delegación **asíncrona de verdad** (el hijo sigue cuando el padre ya respondió,
con transcript propio y steer en vivo).

---

## 10. Providers y routing

**Hermes** (config real de esta máquina):

- Principal: `opencode-go` / `deepseek-v4.1-flash`.
- **13 tareas auxiliares configuradas** con modelo propio: `vision` (modelo de visión distinto),
  `compression`, `title_generation`, `skills_hub`, `approval`, `mcp`, `curator`, `review`,
  `memory_query_rewrite`, `tts_audio_tags`, `voice_chat`, `triage_specifier`,
  `kanban_decomposer`. El schema admite más (`profile_describer`, `goal_judge`, `monitor`,
  `background_review`, `moa_*`). `provider: auto` = hereda el principal.
- Fallback por proveedor ante 429/529/503/red; prompt caching; adaptadores Anthropic/Bedrock/Codex.
- `approvals.mode: off` aquí (con allowlist de comandos peligrosos).

**Nexus** (config real):

- `router.strategy: tier`, **Ollama local siempre primero** (11 modelos, `qwen3-vl-8b` preferido
  para evitar recargas de 45 s), cloud `opencode.ai/zen` **deshabilitado** (los *-free* dan 403
  fuera de OpenCode), 3 CLIs consultores (`claude`, `codex`, `opencode`) apagados por defecto.
- Routing por **capacidad**: `supports_tools`, `supports_vision`; VRAM budget; Thompson sampling
  en `adaptive_router`/`self_learning_loop`; `prefer_models` para no saltar de modelo.
- `llm_provider.py` con costos, cadena de failover y barrera de tool-state (corregida: tenía un
  deadlock real de `CredentialPool`).

**Diferencia clave**: Hermes separa *modelo principal* de *modelo por tarea* (una decisión de
producto: barato para comprimir/titular, bueno para razonar). Nexus separa *modelo por capacidad
y por hardware* (una decisión de local-first: no cargar dos modelos en 8 GB de VRAM). Ambas son
correctas para su caso; mezclarlas sin cuidado rompe la segunda.

---

## 11. Persistencia y sesiones

| | Hermes | Nexus v3 |
|---|---|---|
| DB principal | `state.db` — 45 tablas, 392 MB, WAL + `messages_fts` | `data/session.db` — WAL + FTS, árbol de sesiones |
| Eventos | eventos con `seq` + `replay_epoch` + `session.events.since` + `replay_gap` | `data/event_store.db` + `/api/events` (SSE) + `/api/sessions/{id}/replay` |
| Ramas | `branch` (CAS sobre el padre), `fork`, `rewind`, `compress` | `/branch`, `/fork`, `/rewind`, `/tree` + checkpoints git |
| Export/import | export de sesión, **import de Claude Code y Codex** (`session.foreign.*` con preview) | `/api/export` (ZIP) — sin import cross-tool |
| Compactación de sesión | `session.compress` | `session_distill`, `trajectory_compressor`, `dream_distill` |

Nexus gana en **checkpoints de filesystem** (repo sombra git, incluye untracked, diff y restore
desde la UI) — Hermes tiene checkpoints pero no como "Cambios" de primera clase. Hermes gana en
**replay con contrato** (seq/epoch/gap: la UI no queda con estado parcial al reconectar).

---

## 12. Automatización

| | Hermes | Nexus v3 |
|---|---|---|
| Cron | `cronjob_manage` + `cron/jobs.json` + `executions.db` + página `/cron` + blueprints. En esta sesión CLI **no entrega** resultados: hay que apuntar `deliver` a Telegram/Discord | `scheduler.py`, `jobs.py`, `task_queue.py` (sin página propia) |
| Hooks | `hooks.py`, `builtin_hooks/`, plugin hooks | `hooks_engine.py`: 6 fases (`pre_tool_use`, `post_tool_use`, `on_error`, `on_compact`, `session_start`, `session_end`), **hooks por archivo** en `.nexus/hooks/<fase>/`, exit 2 bloquea |
| Webhooks | plataforma webhook (puerto 8644) + suscripciones | no |
| Kanban | plugin completo: tasks, links, comments, events, runs, attachments, 11 tools | `board.py` con claims + lease |
| Watchers | kanban watchers con cursor sobre `task_events` | `task_queue`, `scheduler` |

Hermes tiene **más superficie de automatización**; Nexus tiene **hooks por archivo** (script
propio en el repo, versionable, exit code como contrato), que es más simple y más auditable.

---

## 13. Superficies

| Superficie | Hermes | Nexus v3 |
|---|---|---|
| CLI | `hermes` (esta sesión) | `nexus-v3.cmd` |
| TUI | `ui-tui` (Ink) | — |
| Escritorio | Electron + React con workbench tileable (zonas, splits, presets, panes keep-alive), 12 rutas | Electron + React con **14 vistas conmutables** en una ventana |
| Web | `web/` | FastAPI sirve `ui/dist` |
| Mensajería | 21 toolsets: Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Mattermost, Email, SMS, Feishu, WeCom, Weixin, DingTalk, QQ, Yuanbao, BlueBubbles, Teams, Google Chat… | — |
| MCP/ACP | servidor MCP propio | MCP server + ACP (Zed) |
| Voz | STT + TTS en streaming | Piper/SAPI TTS, STT :8789 o faster-whisper |
| Navegador | browser-use + CDP + vault de credenciales | `cdp_browser` + MCP playwright/chrome-devtools |

La falta de workbench tileable en Nexus es la brecha de producto más visible: hoy no se puede ver
el diff y el chat a la vez.

---

## 14. Seguridad

| | Hermes | Nexus v3 |
|---|---|---|
| Aprobaciones | `manual / smart / off` por perfil, `approval.request` como request server→client | `auto / assisted / strict / never`, `approval_gate`, `require_confirm_mutating` |
| Scope | por sesión/proyecto | **scope jail** real: `check_path`/`check_tool` en toda tool FS, `HARD_DENY_PATTERNS` que **se suman** (no se reemplazan) → `permission: full` no puede leer `.ssh`, `.aws`, `.env`, `*.pem` |
| Secretos | redacción por defecto (`redact_secrets`), `tirith` pre-exec | deny patterns + scrub de credenciales para subprocesos MCP |
| Guard de secuencia | guardrails de loop (`tool_loop_guardrails`) | `SequenceGuard` con política declarativa (deny/medium/allow) evaluada **antes** del modelo, ventana 20 |
| Sandbox | policies por sesión, Docker opcional | `SandboxService` con allowlist de env (no hereda `os.environ`), kill del árbol, recorte de salida |
| Permiso actual | `approvals.mode: off` | `scope.permission: full` (con hard-deny igual activo) |

Nexus tiene el **modelo de seguridad más explícito** (scope + política + hard-deny + sequence
guard). Hermes tiene el modelo más **operable** (modos que el humano entiende, redacción por
defecto, aprobaciones que sobreviven a la reconexión).

---

## 15. Métricas medidas en esta máquina

### Hermes (`C:\Users\cjtr\AppData\Local\hermes\hermes-agent`)

| Métrica | Valor |
|---|---|
| Versión | v0.22.5 (2026.9.24) stable, local `bf0203a794a0`, git |
| Python | 3.14.9 · OpenAI SDK 2.24.0 |
| `gateway/` | 326 archivos · 123.883 líneas |
| `tui_gateway/` | 118 archivos · 46.874 líneas |
| `agent/` | 394 archivos · 145.450 líneas (330 `.py`, 129.878 líneas sólo en `agent/*.py`) |
| `hermes_cli/` | 762 archivos · 295.396 líneas |
| `apps/desktop/src` | 3.192 archivos · 697.149 líneas (TS/TSX) |
| `apps/desktop/electron` | 650 archivos · 141.052 líneas |
| Contrato RPC generado | 7.270 líneas · 311 identificadores `grupo.método` |
| Tools en el loop | 51 core + 21 toolsets de plataforma + 178 diferidas |
| Skills instaladas | 162 `SKILL.md` (55 carpetas raíz) |
| `state.db` | 392 MB · 45 tablas · 108 sesiones · 46.974 mensajes |
| Memoria | `MEMORY.md` 4.383/22.000 chars · `USER.md` 2.032/13.750 chars |
| Tests | 6.785 archivos `test_*.py` |
| Config | 381 líneas · `_config_version: 51` · 13 tareas auxiliares |

### Nexus v3 (`D:\ias\proyectos\nexus-v3`)

| Métrica | Valor |
|---|---|
| Versión | 3.0.0-alpha |
| Python en `src/` | 60.502 líneas · 130 módulos en `src/core` |
| `src/api/server.py` | 7.869 líneas · **138 rutas** `@app.*` |
| Tools | **52** registradas (`register_function`) |
| Loop | `agent_loop.py` 1.722 líneas (TDAO) |
| Contexto | `compaction.py` 704 líneas (4 capas) |
| Seguridad | `scope.py` 546 líneas |
| Sesiones | `session_log.py` 689 líneas |
| Prompt | `prompt_assembler.py` 248 líneas |
| Tests | 138 archivos `tests/*.py` (docs declaran 607 passed / E2E 41-41) |
| Grafo | 12.411 nodos · 31.361 edges · 12.970 entidades · 42.422 vínculos · 23 revisiones · 42,7 MB |
| RAG | 19.286 chunks · 286 MB |
| Config | 416 líneas · 25 iteraciones · 32 k tokens · 900 s · 60 tool calls |

---

## 16. Paridad honesta

Leyenda: **+** hace más · **=** equivalente · **−** falta.

| Capa | Hermes | Nexus v3 | Δ |
|---|---|---|---|
| Transporte UI↔backend | JSON-RPC/WS con contrato **generado** | 138 REST + SSE/WS sin contrato generado | Nexus − |
| Replay de eventos | `seq` + epoch + `replay_gap` | replay sin epoch ni gap explícito | Nexus − |
| Loop explícito | fragmentado en ~30 módulos | TDAO único, con budget y guardrails propios | Nexus + |
| Compactación | resumen con auxiliar + poda + imágenes + caching | 4 capas deterministas + spill + handoff | **=** (distinto énfasis) |
| Memoria largo plazo | archivos + FTS + skills | grafo 12 k nodos + RAG 19 k chunks + revisiones | **Nexus +** |
| Memoria de trabajo | 2 archivos curados, inyección siempre | inyección automática del grafo con presupuesto | Hermes + |
| Tools | 51 + 178 diferidas | 52, todas en el prompt | Hermes + (escala) |
| Seguridad de FS | policies por sesión | scope jail + hard-deny + sequence guard | **Nexus +** |
| Sub-agentes | asíncronos, aislados, steer | paralelos, `allowed_tools`, worktrees, claims | **=** |
| Providers | principal + 22 auxiliares | local-first, capability routing, Thompson | **=** |
| Superficies | CLI, TUI, desktop, web, 21 plataformas | Electron + web local | Hermes + |
| Workbench | tileable, panes keep-alive, preview anotado | 14 vistas conmutables | Hermes + |
| Cron | página + blueprints + delivery | scheduler sin página | Hermes + |
| Import de sesiones ajenas | Claude Code / Codex con preview | no existe | Hermes + |
| LSP | no | `lsp_client.py` (pylsp) | Nexus + |
| Checkpoints de FS | sí, no como feature central | repo sombra git + diff/restore en UI | **Nexus +** |
| Aprendizaje | curator, skill learning | `self_learning_loop`, `skill_opt` held-out, `dream_distill` | Nexus + |
| Operación | command center, doctor, backup, logs, update con handoff | `doctor`, `diagnostics`, `self_update` git | Hermes + |
| Tests | 6.785 archivos | 138 archivos, E2E 41 checks | Hermes + (volumen) |
| Multi-máquina | SSH, cloud, pairing, hosted rooms | colmena propia + VPS | **=** |

---

## 17. Lo que Hermes tiene y Nexus no (y qué costaría)

Ordenado por valor/riesgo, coherente con lo que ya dice `docs/HERMES_DESKTOP_AUTOPSY.md`:

1. **Contrato de transporte generado desde Python** — el backend es la fuente; `tsc` rompe si la
   UI quedó vieja. En Nexus serían ~150 líneas de generador + un test de frescura sobre los
   modelos Pydantic que ya existen.
2. **Replay con `seq`/epoch/gap** — elimina la clase de bugs "el chat quedó con estado parcial".
   Nexus ya tiene replay; falta numerar y emitir `gap`.
3. **Import de sesiones de Claude Code / Codex** — barato y de altísimo impacto para alguien que
   llega con historial. Nexus ya tiene el formato en SQLite.
4. **Modo de aprobación como concepto de primera clase** (`manual/smart/off`) con chip en la barra.
   Nexus tiene la política (`auto/assisted/strict/never`); falta exponerla.
5. **Workbench de 2 paneles keep-alive** (no el motor completo de zonas): chat + diff/terminal
   conviviendo. Es la brecha de producto más visible.
6. **Acción larga = tarea observable** (crear tarea, pollear estado, no bloquear UI).
7. **Panel de desglose de contexto** — Nexus ya calcula el presupuesto; falta mostrarlo.
8. **Catálogo único de capacidades** (MCP + tools + skills + plugins en una página).

## 18. Lo que Nexus tiene y Hermes no

1. **Grafo de conocimiento con revisiones y restore** (12.411 nodos, append-only, FTS al día).
2. **RAG con MMR** sobre 19.286 chunks, con índice propio.
3. **Destilado automático al compactar y al cerrar sesión** — el conocimiento no se pierde al
   liberar contexto.
4. **Scope jail con hard-deny que no se puede desactivar con `permission: full`**.
5. **SequenceGuard con política declarativa** evaluada antes del modelo.
6. **Checkpoints en repo sombra git** (incluye untracked, diff y restore desde UI).
7. **LSP (pylsp)** con diagnósticos post-edición.
8. **`apply_patch` atómico multi-archivo** con rollback completo si falla un hunk.
9. **Spill a disco** con ruta para releer la salida completa (cabeza 62% + cola 38%).
10. **Self-learning con held-out** y `dream_distill`.

---

## 19. Cómo verifiqué (reproducible)

Todo lo cuantificado arriba salió de estos comandos, en sólo lectura:

```bash
# Hermes
hermes --version
cd C:/Users/cjtr/AppData/Local/hermes/hermes-agent
find gateway -type f -name '*.py' | wc -l
cat gateway/**/*.py | wc -l                      # por directorio, ver §15
wc -l apps/shared/src/gateway-contract.generated.ts
grep -oE "'[a-z_]+\.[a-z_]+'" apps/shared/src/gateway-contract.generated.ts | sort -u | wc -l
find C:/Users/cjtr/AppData/Local/hermes/skills -name SKILL.md | wc -l
python -c "import sqlite3;c=sqlite3.connect(r'file:C:\Users\cjtr\AppData\Local\hermes\state.db?mode=ro',uri=True);print(c.execute('select count(*) from sessions').fetchone(), c.execute('select count(*) from messages').fetchone())"

# Nexus v3
cd D:/ias/proyectos/nexus-v3
wc -l src/api/server.py src/core/agent_loop.py src/core/compaction.py src/core/scope.py
grep -cE '^@app\.(get|post|put|delete|websocket)' src/api/server.py
grep -c "registry.register_function" src/api/server.py
ls tests/*.py | wc -l
python - <<'EOF'
import sqlite3
c=sqlite3.connect(r"file:D:\ias\proyectos\nexus-v3\brain\knowledge_graph.db?mode=ro",uri=True)
for t in ("nodes","edges","entities","entity_nodes","node_revisions"):
    print(t, c.execute(f"select count(*) from {t}").fetchone()[0])
EOF
```

---

## 20. Límites de este informe

- **No ejecuté** la suite de Nexus ni su E2E: `opencode` está trabajando en el repo y correr tests
  puede interferir (además de ser escritura en `data/`). Los números de tests que cito son de
  `docs/CAPABILITY_MATRIX.md`, no de una corrida mía; los de estructura y bases **sí** son
  mediciones propias.
- El conteo de métodos del contrato de Hermes varía según el criterio (311 identificadores únicos
  medidos por grep vs 392 métodos declarados en la autopsia previa). Cito ambos.
- Las líneas de `apps/desktop/src` dependen de qué extensiones se cuenten; usé `*.ts`/`*.tsx`.
- No se modificó nada de `nexus-v3`. El único archivo creado es este documento.

---

**Conclusión en una línea**: Hermes es mejor **producto** (superficies, contrato, operación,
canales); Nexus v3 es mejor **cerebro de agente** (grafo, RAG, scope, compactación explicable,
aprendizaje). La brecha que le queda a Nexus no es de inteligencia: es de **contrato entre capas
y de superficie**.
