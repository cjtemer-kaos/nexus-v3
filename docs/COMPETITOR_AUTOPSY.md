# Autopsia de competidores → qué adoptar en nexus-v3

Fuente: análisis de `D:\ias\autopsia\{opencode, hermes-agent, deepseek-harness}`.
Método: agentes de exploración (opencode=Go, hermes=Python) + **REA** (`analyze_javascript_application`) para deepseek-harness (TS) + agente.
Fecha: 2026-10-12.

---

## 0. Resumen ejecutivo (qué hacen mejor)

| Repo | Naturaleza | Fortaleza clave |
|---|---|---|
| **opencode** (Go) | TUI agent, Bubble Tea | **LSP-in-the-loop**, apply-patch estilo Codex, diff engine, permission channel, event bus, prompt-caching Anthropic |
| **hermes-agent** (Python) | Harness gigante multi-plataforma | **Compresión de 2 niveles**, toolset distributions, **ACP** (IDE interop) + **MCP serve**, state design, gateway multi-transporte, cron |
| **deepseek-harness** (TS) | Harness "todo-plugin" (Cordis) | **Event-sourced log** ("model-visible ⟺ logged"), projections, **waterfalls + monotonic guard**, scheduler paralelo fail-closed, sandbox argv-wrap, ACP, self-modification |

**Patrón común 2026:** el modelo es ~1-2% del sistema; la ventaja está en **contexto, permisos, recuperación, evals y extension points**. Los 3 son **event-sourced / cache-aware / sandbox-first**.

---

## 1. opencode (Go) — mecanismos a adoptar

| Mecanismo | Detalle (evidencia) | → nexus-v3 | Esf. |
|---|---|---|---|
| **LSP-in-the-loop** | diagnostics se **inyectan al resultado de la tool** (`edit.go:166`, `write.go:215`, `view.go:177`) en tags `<file_diagnostics>`; `waitForLspDiagnostics` espera 5s; el system prompt explica los tags (`coder.go:197`) | cerrar gap #6 (LSP). Nuevo `src/tools/lsp_tool.py` + hook post-tool que adjunta diagnostics | L |
| **apply-patch estilo Codex** | `diff/patch.go`: parser `*** Begin Patch`, context matching con fuzz, multi-archivo atómico, rechaza rutas absolutas (`:694`) | mejorar tool `edit` → soportar patch multi-hunk | M |
| **Diff engine** | `diff/diff.go`: side-by-side, intra-línea (diffmatchpatch), Chroma highlight | gap #1 (diff UI) — `ui/components/editor/DiffView.tsx` | M |
| **Permission channel** | `permission.Request` **bloquea** la tool en un channel mientras publica evento a la UI; grants one-shot / session-persistent (path normalizado) (`permission.go:74-112`) | gap #20 — `scope` + `approval_gate` | S |
| **Event bus** | `Broker[T]` tipado, publish non-blocking drop-on-full, bridge a TUI (`cmd/root.go:249`) | nexus tiene `event_store`; añadir broker→UI | S |
| **Prompt caching Anthropic** | cache breakpoints en system + **última tool def** + últimas 3 msgs (`anthropic.go:135-194`) | gap #7 — provider cloud | M |
| **Persistent shell** | shell global con worker, `cd` persiste, mata hijos en timeout (`pgrep -P`+SIGTERM) (`shell/shell.go`) | gap #8 (PTY) — reusar `persistent_shell.py` | M |
| **Subagent + cost rollup** | child session = tool-call id; costo del hijo suma al padre (`agent-tool.go:62`) | mejora `_helper_run` | S |
| **Read-before-edit + mtime** | evita ediciones stale (`edit.go:266`) | adoptar en tool `edit` | S |
| **Reasoning gating** | "think" en el último user → thinking budget 80% (`anthropic.go:162`) | provider | S |

## 2. hermes-agent (Python) — mecanismos a adoptar

| Mecanismo | Detalle | → nexus-v3 | Esf. |
|---|---|---|---|
| **Compresión de 2 niveles** | live (`context_compressor.py`: prune tool-results sin LLM → proteger head/tail por tokens → resumen estructurado con modelo aux barato → rotación de sesión con lineage) + offline (`trajectory_compressor.py`) | nexus tiene compaction; adoptar prune-first + resumen estructurado + **session lineage** | M |
| **Toolset distributions** | `{toolset: prob%}` muestreado por run (`toolset_distributions.py:29`) | nuevo: exponer superficies de tools por perfil | M |
| **ACP adapter** | `hermes-acp` = agente ACP sobre stdio; per-session agent, `contextvars.copy_context()`, callback→session_update, deque FIFO por nombre de tool, todo→plan panel (`acp_adapter/`) | **alto valor** (IDE interop) — nexus tuvo `acp_protocol.py` (purgado) | L |
| **MCP serve** | expone conversaciones como tools MCP + `EventBridge` long-poll (`mcp_serve.py:450`) | nexus solo tiene MCP cliente → agregar **MCP server** | M |
| **State design** | reconciliación declarativa de schema (sin migraciones por versión), `BEGIN IMMEDIATE`+retry jitter, WAL fallback, **dual FTS5** (unicode61+trigram CJK) (`hermes_state.py:464,375,128,253`) | mejorar `session_log` | M |
| **Prompt-cache discipline** | system prompt byte-stable (construido 1 vez, releído de DB); plugin context/skills van en **user** msg | gap #7 | S |
| **CommandDef registry** | una lista central alimenta CLI/gateway/Telegram/Slack/autocomplete/help | nexus `slash_commands` (centralizar) | S |
| **Hooks vocabulary** | 18 hooks (pre/post tool, pre/post LLM, transforms, session lifecycle, approval) + `ctx.llm` facade + `tool_override` | ampliar `hooks_engine` | M |
| **Gateway + cron** | un agente, N transportes; cron con **modo `no_agent`** (script stdout = job) | nexus `scheduler` | M |
| **Guardrails** | halt por tool repetida sin progreso; file-mutation verifier footer; sanitización de errores antes de re-inyectar | mejora `agent_loop` | S |

## 3. deepseek-harness (TS, Cordis) — mecanismos a adoptar

| Mecanismo | Detalle (evidencia) | → nexus-v3 | Esf. |
|---|---|---|---|
| **Event-sourced log** | invariante **"model-visible ⟺ logged"**; historial = `deriveMessages()` del log, nunca lista mutable (`core/session/src/index.ts`) | **alto valor** — base de `session_log`; adoptar derive | M |
| **Session projections** | fold incremental `stateOf(session,key)` para turnBoundary/todos/permissions | nuevo | M |
| **Capability seam tri-rol** | Definition/Provider/Consumer; registros = effects descartables; múltiples providers tras un registry | arquitectura (multi-provider) | L |
| **Tool output contract** | `{schema, render, presentationMeta}` + freeze lossless JSON + validación (`tools/src/types.ts:204`) | mejora `tool_registry` | S |
| **Waterfalls + monotonic guard** | `tools/pre-execute|execute|post-execute`, `agent/pre-step|request|request-error`, `llm/stream`; **guard deny-only irre-allowable** (`tools/src/index.ts:1091`) | ampliar `hooks_engine` + primitivo de seguridad | M |
| **Scheduler paralelo fail-closed** | pool bounded (maxParallel=10), re-clasifica cada call (barrera exclusive), commit en orden del modelo, resultados sintéticos ABORTED para replay (`tool-calls.ts`) | mejorar pre-dispatch paralelo de nexus | M |
| **Compaction KV-aware** | lock durable `compaction/start`, pruner sin LLM primero, resumen reusa prefix (cache), recovery en `agent/request-error` | mejora `compaction` | M |
| **Continuable subagents** | model de Activation por sesión durable; parent/child authority; cold resume | mejora `sub_agent_spawner` (hoy muerto) | L |
| **Sandbox argv-wrap** | `confine(argv, policy) → ConfinedArgv`; modos read-only/workspace-write/danger-full; clasifica **denial vs runner-failure**; fail-closed (`docs/subsystems/sandbox.md`) | mejora `sandbox_service` | M |
| **Hook-compat bridges** | `hooks-claude-code`/`hooks-codex` mapean hooks de esos agentes a los mismos extension points | reutilizar ecosistema de hooks | S |
| **Profiles/bundles** | capas ordenadas con patch; **todo (incluido el loop) reemplazable** | mejora `config` | M |
| **Self-modification / PTC** | `tool-cordis`: el modelo define/monta plugins en runtime; PTC colapsa el catálogo tras `run_code` (SDK tipado) | avanzado (requiere sandbox+approval) | L |
| **ACP server** | server ACP automation-only (initialize/session/new/list/resume/prompt/cancel) | IDE interop | L |

---

## 4. Steal list priorizada (top 10, por ROI para nexus)

1. **LSP-in-the-loop** (opencode) — el mayor salto de calidad de edición; cierra gap #6. `src/tools/lsp_tool.py` + hook post-tool.
2. **Diff + review inline** (opencode/deepseek) — gap #1, mecanismo #1 de confianza. `DiffView.tsx`.
3. **Event-sourced session log + derive** (deepseek) — base para fork/rewind/replay y proyecciones.
4. **Permission channel + grants** (opencode) — gap #20; hoy el gate mutating es no-op.
5. **Prompt-cache discipline + breakpoints** (hermes/opencode) — gap #7; mayor palanca de costo.
6. **Compaction prune-first + KV-aware + session lineage** (hermes/deepseek) — mejora directa de `compaction`.
7. **ACP adapter** (hermes/deepseek) — IDE interop ("hive"); nexus lo tuvo y se purgó.
8. **Scheduler paralelo fail-closed** (deepseek) — mejora el pre-dispatch actual (que corta al 1er mutante).
9. **apply-patch + read-before-edit** (opencode) — edición multi-hunk segura.
10. **MCP serve (inbound)** (hermes) — exponer nexus como MCP server a otros agentes.

---

## 5. Advertencias / anti-patrones vistos

- **openace DB write amplification**: persiste el assistant en cada delta de token (`agent.go:456`) → debounce en nexus.
- **Hermes: `run_agent.py`/`cli.py` son fachadas gigantes** → mantener el core extraído (nexus ya lo hace).
- **deepseek: no compat de `SessionFormatVersion`** → versionar el log desde el día 1.
- **No confiar en un analyzer Electron para árboles Node/TS** (REA devolvió todo-cero para `packages/core`).
- **Sin git worktrees reales en opencode** (pese al prompt) → nexus debe implementarlos de verdad.
- **Prompt-cache**: nunca reescribir mensajes viejos mid-run (rompe prefijo).

---

## 6. Cline (`cline/cline`, commit `e9dafbb`, 2026-10-21)

Autopsia completa: **[`AUTOPSY_CLINE.md`](./AUTOPSY_CLINE.md)** (monorepo `sdk/packages/*`; `apps/vscode/src` es legacy).

Top 5 por ROI para nexus-v3:

1. **`buildBudgetProjection`** (`sdk/packages/core/src/extensions/context/budget-projection/project.ts:479`): closure de pares `tool_use`/`tool_result`, live tail protegido, drop de `thinking`/binarios, truncado proporcional y auditoría de acciones. Gap real: la compactación de nexus conserva “últimos N” y puede perder el pedido actual.
2. **Trigger 90% + corrección por subestimación** (`context/compaction.ts:342-362`): factor `actual/estimado` cap 4x usando `usage.prompt_tokens`.
3. **Overflow recovery determinista** (`compaction.ts:480-543`): fallback `basic` sin LLM y validación “estrictamente menor” antes de reintentar.
4. **`apply_patch` difuso** (`tools/executors/apply-patch-parser.ts:85`): Add/Delete/Move + cascada exacta→NFC→trim→similitud 0.66, warnings por chunk, atomicidad.
5. **Hooks por archivo** (`hooks/hook-file-config.ts:17`): scripts `PreToolUse.py`/`PostToolUse.ps1`/… con payload JSON y control `cancel/context/systemPrompt/appendMessages`.

Ya portado en el lote 2026-10-21: **proyección de presupuesto** (`src/core/compaction_budget.py`,
integrada en `compaction.py`), **`apply_patch` difuso** con Add/Delete/Move y rollback
(`src/core/apply_patch.py` + `server.py`), **`command-guard` de plan mode**
(`src/core/command_guard.py` + `scope.py`), **calibración por `usage.prompt_tokens`** y
**recovery que exige un request menor** (`agent_loop.py` + `compaction.py`), **kill de árbol
de procesos** (`src/core/process_tree.py`), barrera de paralelismo por prefijo contiguo,
diff numerado en `edit` y truncado head+tail en `exec`/`read_file`. Detalle, invariantes y
tests en **[`AUTOPSY_CLINE.md`](./AUTOPSY_CLINE.md)** §8.

En el segundo lote se cerraron los 6 pendientes: **hooks por archivo**
(`src/core/file_hooks.py`), **checkpoints git con untracked** en repo sombra
(`src/core/git_checkpoints.py`), **estado de compactación persistido**
(`compaction_state` en `session.db`), **team tools** (`src/core/team.py`),
**UI de `apply_patch`** (`ApplyPatchResult.tsx`) y **output-limits en search/glob**
(`cap_json_list`). Detalle, invariantes y tests en
**[`AUTOPSY_CLINE.md`](./AUTOPSY_CLINE.md)** §8 y §10.
