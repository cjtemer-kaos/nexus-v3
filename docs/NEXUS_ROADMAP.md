# Nexus v3 — Roadmap para igualar/mejorar la competencia

Síntesis de 4 investigaciones paralelas (arquitectura de nexus, interior de la competencia, playbook de harness + análisis de moat, UX/IDE).
Fecha: 2026-10-12. Estado: **propuesta** (pendiente de aprobación).

---

## 1. Veredicto rápido

Nexus es un **harness fuerte** (loop TDAO, compactación 4 capas, scope jail, routing local+cloud, grafo OPCODE, self-learning).
Lo que le falta vs Cursor/Claude Code/Cline está **concentrado en el loop de ingeniería de software**:

> No hay **diff/approve**, **checkpoints/undo**, **git**, **test-runner**, **AGENTS.md**, **LSP**, **multi-file apply**, **PTY real**, ni **code-indexing** cableado.
> Y hay mucha **orquestación sofisticada construida pero sin cablear** (multi-agente, plan mode, RAG-engine, skills).

Regla de la industria (2026): **el modelo es ~1-2% del sistema**; la ventaja está en permisos, contexto, recuperación y **evals**. Invertir ahí.

---

## 2. Table stakes (para ser creíble) — estado en nexus

| # | Requisito | Nexus | Evidencia / gap |
|---|---|---|---|
| 1 | Loop ReAct con terminación tipada + guards | ✅ | `agent_loop.py` (TDAO, budget, doom-loop) |
| 2 | Tools concurrentes (reads paralelos, writes serial) | ⚠️ | `_READONLY_TOOLS` paralelo, pero `break` al 1er mutante (`agent_loop.py:780`) |
| 3 | read(range+outline), edit, write, grep, glob, bash, web | ⚠️ | read/write/edit/exec/web_fetch/web_search ✅; **falta grep/glob dedicados y read con rango** |
| 4 | Edición confiable (str_replace/apply-patch) | ✅ | tool `edit` (recién agregada) |
| 5 | **Snapshots pre-edit + undo/rewind** | ❌ | no hay; `_attic/checkpoint.py` sin cablear |
| 6 | Compactación por capas, barato-primero | ✅ | `compaction.py` (4 capas) + constraint-pinning pendiente |
| 7 | Historial append-only cache-friendly | ⚠️ | compaction reescribe; sin métrica de cache-hit |
| 8 | **AGENTS.md jerárquico (nearest-wins)** | ❌ | solo system prompt hardcodeado (`agent_loop.py:453`) |
| 9 | Permisos con default seguro + autonomía graduada | ⚠️ | `scope` + `permission`; gate mutating es **no-op** (`scope.py:232`) |
| 10 | **Sandbox de SO real** | ⚠️ | `sandbox_service` (subprocess) sin Seatbelt/bwrap/MXC |
| 11 | Sesiones: resume/fork/rewind | ⚠️ | fork/branch ✅; **rewind ❌** |
| 12 | **Git nativo** (status/diff/commit/PR) | ❌ | solo vía `exec` |
| 13 | Routing/BYOK/per-rol | ✅ | `provider_router` + `provider_catalog` |
| 14 | MCP cliente (stdio+HTTP/SSE) | ✅ | `mcp_client`; pero tools MCP **no de 1ª clase** |
| 15 | **Tests/lint/build en el loop** | ❌ | solo `exec` |
| 16 | Modos plan (read-only) vs act | ⚠️ | `/plan` solo flipea `read_only`; `plan_mode.py` sin construir |

---

## 3. Roadmap priorizado (P0 → P2)

### P0 — table stakes (sin esto no competís)
1. **Sesión unificada** (workspace + worktree + chat + cambios + status) — spine de la UI.
2. **Diff review inline** (Keep/Undo por hunk y por archivo; lista de cambios). *El mecanismo #1 de confianza.*
3. **Motor de permisos/approval** (ask/allow/auto + sandbox + reglas por tool/URL/cmd).
4. **Plan mode real**: investigar → plan editable → aprobar → ejecutar.
5. **Checkpoints / rewind / fork** (snapshot por turno).
6. **Selector de modelo con badge local/cloud** + routing por rol.
7. **Panes PTY + editor + agente** coexistencia (terminal real con xterm).
8. **Explorer de toda la PC con frontera explícita** (read System vs Workspace + allowlist + protegidos).

### P1 — diferenciadores
- Agentes paralelos por **git worktree** + superficie de orquestación.
- **Board de tareas + claims + review packets** (revisión humana como gate).
- **Grafo de conocimiento citable** (nodo → código, con decay y validación).
- Sub-agentes como **cards anidadas** (contexto aislado, resumen al padre).
- **Skills + MCP** con descubrimiento progresivo y trust por fuente.
- **Instrucciones de proyecto** (AGENTS.md/CLAUDE.md, path-scoped) con preview del set efectivo.
- **Review gates** (feedback→agente vs comentario→PR; reviewer agent en contexto limpio).
- **Queue/steer/stop** mid-run.
- Handoff local↔cloud/background + artefactos.
- Búsqueda/resume de sesiones + import cross-tool.
- Canvas espacial (opcional).
- **Design Mode** (prompting visual).

### P2 — polish
Context/cost meter, best-of-N/Arena, automations, voz, agent-cursor, onboarding/import, remote/TUI, hooks.

---

## 4. Quick wins (bugs hallados por el análisis — arreglar YA)

| Bug | Evidencia | Fix |
|---|---|---|
| **Búsqueda global rota** | UI llama `/api/sessions/global/search` (no existe) → siempre vacío | agregar endpoint global FTS |
| **Slash endpoints colgados** | `/session list`→`/api/sessions/catalog`, `/export`→`/api/sessions/{id}/export` no existen | crear endpoints |
| **Dos routers divergentes** | `ProviderRouter._stats` in-memory vs `AdaptiveRouter` persistido | unificar: `chat()` alimenta Thompson |
| **`_READONLY_TOOLS` fantasma** | incluye `code_search` (no registrada) | registrar o quitar |
| **HINT_MAPPING incompleto** | faltan `design/music/debug/test/security/devops/train/prompt` (tier 0) | completar |
| **Gate mutating es no-op** | `scope.py:232` | implementar confirmación |
| **NexusHive hardcodea :9000** | PC2 ya no tiene :9000 | parametrizar |
| **TodoStore abre DB por turno** | `server.py:2684` | singleton |
| **Código muerto** | `trajectory_compressor`, `session_manager`, `bounded_history`, `plan_mode`, `sub_agent_spawner`, `nexus_hive`, `dag_coordinator`, `agent_runner` sin cablear | cablear o archivar |

---

## 5. Moat — análisis honesto (importante)

Ninguno de los 4 diferenciadores es defendible **tal como se enuncia**. El moat real es un **flywheel compuesto** (tiempo + datos + feedback + integración + gobernanza). La pregunta que manda: **¿cuánto le cuesta a un cliente irse?**

| Claim | Defendible? | Qué lo hace real (no gimmick) | Métrica de prueba |
|---|---|---|---|
| **Aprendizaje en vivo** | Medio (literatura abierta) | loop de feedback **propietario y verificado** (tests/prod/humano) + guardrails duros (no judge-solo) | mejora en eval **congelado**; tasa de forgetting; **reward-hacking rate** |
| **Memoria grafo "infinita"** | Débil tal cual | **corrección temporal** (bitemporal), conflict resolution, decay, **citas a código**, gobernanza | LongMemEval/**MemoryArena** a token fijo; contaminación por stale; **costo de irse** |
| **Local + cloud** | Débil (commodity) | routing **calibrado** (ECE), gating de privacidad, por-subtarea, cache-locality | costo/query a calidad fija; % on-device; p99 |
| **Colmena (cualquier programa)** | Débil (MCP/A2A son estándar) | capa de **trust/gobernanza** (catálogo verificado, ACL por tool, sandbox, auditoría, idempotencia) | éxito cross-app en benchmark **bloqueado**; incidentes/1000 calls |

**Conclusión del moat:** invertir en (a) **evals continuos** (incidentes → tests de regresión), (b) **memoria con citas + decay + gobernanza**, (c) **routing calibrado**, (d) **capa de confianza para MCP/hive**. Eso compone; "conectar a todo" no.

---

## 6. Anti-patrones a evitar (documentados)

1. Coordinación por lock-file entre agentes (deadlocks) → usar ownership/handoffs.
2. Integrador/merge-gate central (cuello de botella) → handoffs.
3. Loop infinito compact-fail-retry → circuit breaker + reactive-compact one-shot.
4. Sobrecarga de tools (>20 degrada) → tool-search/descubrimiento progresivo.
5. Reescribir mensajes viejos mid-run (rompe el cache) → append-only.
6. Whole-file edit por defecto → diff/str_replace.
7. Instrucciones en la conversación (se pierden al compactar) → AGENTS.md re-inyectado.
8. Índice de código stale (wild-goose-chase) → refrescar en writes del agente.
9. Terminal sin sandbox por defecto → sandbox default, red off.
10. Contexto sin techo → auto-compact con buffer + offload de outputs.
11. **Estado de aislamiento oculto** (worktree/sandbox sin badge) → badge siempre.
12. **Status fabricado** → derivar busy/idle de procesos reales.
13. **Auto-apply por defecto** → default manual/assisted.

---

## 7. Orden sugerido

1. **Sprint 0 (esta semana)**: quick wins (§4) — correctitud + limpieza.
2. **Sprint 1**: P0.1 sesión + P0.7 panes + P0.3 permisos + P0.6 modelo.
3. **Sprint 2**: P0.2 diff + P0.5 checkpoints + P0.4 plan mode.
4. **Sprint 3**: P0.8 frontera FS + P1.8 queue/steer + P1.7 review.
5. **Sprint 4+**: worktrees + board/claims/gates + sub-agent cards + grafo citable + AGENTS.md + skills/MCP.

---

## 8. Estado de implementación (2026-10-12) — "termina todo"

Implementado, probado (suite exit 0, `scripts/e2e_diagnostic.py` 41/41) y commiteado:

| Item del roadmap | Qué se hizo | Commit |
|---|---|---|
| Quick wins §4 | HINT_MAPPING, `/api/sessions/global/search`, `/catalog`, `/{id}/export`, `code_search` | e70113f |
| Tools faltantes | `edit` (str_replace), `web_search` (SearXNG+Wikipedia) | 76f884e |
| P0.5 checkpoints | snapshot pre-write/edit + list/restore | 92a9394 |
| P1.7 review / P0.2 diff | `POST /api/diff` + diff de checkpoint | 89072f7 |
| P1.7 review UI | panel **Cambios** (lista + diff verde/rojo + Restaurar) | b01e888 |
| LSP (diagnostics-in-the-loop) | sintaxis rápida + **LSP semántico** (pylsp) → `<file_diagnostics>` | 92a9394, beff580 |
| P0.3 permisos | gate mutante opt-in (`require_confirm_mutating`) + `sandbox.run_argv` con política | 4cc8bda |
| Scheduler | paraleliza todas las read-only (fail-closed), mutantes serial | 92a9394 |
| Interop | **MCP serve** (inbound, 21 tools) + **ACP** (Zed) | 5de2b89 |
| Sub-agentes | `sub_agent` / `sub_agent_message` / `sub_agent_list` (continuable, depth 3) | 5de2b89 |

Pendiente (mayor / requiere UI o refactor): event-sourced session log + derive (fork/rewind),
plan mode, worktrees, board/claims/gates, sub-agent cards en UI, grafo citable, AGENTS.md,
gestión de skills/MCP en UI, queue/steer, status real desde procesos.

## 9. Segunda tanda (2026-10-12) — "no te pares"

Todo implementado, probado (suite exit 0, `e2e_diagnostic` 41/41) y commiteado:

| Item del roadmap | Qué se hizo | Commit |
|---|---|---|
| Session log | `fork`/`rewind`/`tree`/`messages` + endpoints; guard FK con 409 claro | caa757a |
| Event-sourced | `GET /api/events` + `GET /api/sessions/{id}/replay` (vista derivada) | caa757a, dc93386 |
| Plan mode | `POST/GET /api/sessions/{id}/mode` (act\|plan) → el turno corre con scope `read_only` | caa757a |
| Queue/steer | `AgentLoop.run(steer=...)` inyecta mensajes entre iteraciones + `POST /api/sessions/{id}/steer` | caa757a |
| Status real | `GET /api/status` (runs activos + subagentes + modos) + footer en vivo | caa757a, f987c33 |
| Board/claims/gates | `src/core/board.py` (estados + claim con gate de deps fail-closed) + panel **Tareas** | caa757a, f987c33 |
| Worktrees | `src/core/worktree.py` (git worktree add/list/remove) + endpoints | caa757a |
| Skills/MCP UI | panel **Capacidades** (tools + skills) + `GET /api/tools`/`/api/skills` | f987c33 |
| Sub-agent cards | panel **Sub-agentes** (tarjetas en vivo, poll /api/status) | dc93386 |
| Grafo citable | `GET /api/graph/node/{id}/cite` → `[n{id}] title` + source | dc93386 |
| AGENTS.md | guía completa para agentes (estructura, comandos, reglas, puertos) | f987c33 |

Pendiente (grande, requiere diseño propio): unificar routers (`ProviderRouter._stats` vs
`AdaptiveRouter`), AGENTS.md jerárquico por directorio, auto-apply assisted por defecto,
gate real de permisos con UI de aprobación, plan mode con UI de plan editable.

## 10. Tercera tanda (2026-10-12) — cierre total

Todo el "pendiente grande" quedó hecho, probado (suite exit 0, `e2e_diagnostic` 41/41) y commiteado:

| Item | Qué se hizo | Commit |
|---|---|---|
| Unificar routers | ProviderRouter usa el AdaptiveRouter (Thompson persistido) como única fuente de verdad | 7204bf4 |
| AGENTS.md jerárquico | `agents_md.py` inyecta root + ancestros; AGENTS.md anidados (src/core, src/api, ui/src, tests, scripts) | ce13c0b |
| Auto-apply assisted | `agent_loop.auto_apply: auto\|assisted(default)\|approve` | ce13c0b |
| Gate de permisos + UI | `approvals.py` + endpoints + panel **Aprobaciones** (diff preview, aprobar/rechazar) | ce13c0b |
| Plan mode editable | `POST /api/plan` + panel **Plan** (editar pasos y ejecutar) | ce13c0b |

**UI final (11 vistas):** Chat · Ayudantes · Grafo · Editor · Cambios · Aprobaciones · Plan ·
Tareas · Sub-agentes · Capacidades · Terminal.

No queda pendiente "grande" del roadmap. Mejoras opcionales futuras: worktrees por tarea en la UI,
grafo 3D/interactivo, telemetría de costo por sesión, plugins de terceros.
