# Harness de IA: qué reutilizar y qué no (2026-10-20)

Análisis de `D:\ias\autopsia` para cerrar gaps reales de `nexus-v3`. No es un catálogo
de features: cada fila tiene archivo, licencia y límite de uso.

## 0. Método y licencias

- Fuente primero (`rg`, lectura de código). REA se usó solo sobre artefactos
  (ASAR/Electron); para repos fuente completos no aporta y devuelve ruido.
- Se verificó la licencia de cada repo antes de proponer reutilización.
- Regla: se puede adaptar código MIT/Apache-2.0 conservando aviso de licencia; el
  código propietario o GPL-3.0 solo se usa como referencia de comportamiento.

| Repo | Licencia | Qué hay realmente en `D:\ias\autopsia` |
|---|---|---|
| `hermes-agent` | MIT | Agente Python completo (loop, tools, memoria, gateway, ACP) |
| `deepseek-harness` | MIT | Harness TypeScript/Cordis completo (paquetes por capability) |
| `opencode` | MIT | TUI/agente Go completo |
| `goose` | Apache-2.0 | Core Rust + UI Electron; ASAR empaquetado |
| `continue` | Apache-2.0 | Extensión/IDE + core TypeScript |
| `aider` | Apache-2.0 | Agente Python con repo-map tree-sitter |
| `OpenHands` (app/server) | MIT (excepto `enterprise/`) | App/server v1; sin el core del agente |
| `openhands-sdk` | MIT | SDK completo: agente, condensadores, seguridad, eventos, subagentes |
| `claude-code` | Propietaria (“All rights reserved”) | Solo plugins/hooks/docs; el CLI real es un binario nativo de ~254 MB |
| `codex` | Apache-2.0 | Fuente Rust completa (`codex-rs`, 9.379 archivos); el paquete instalado era solo un wrapper |
| `mimo-code` | MIT | Fork de OpenCode: plan mode, permisos, auto-respond |
| `jcode` | MIT | Agente Rust con memoria asíncrona en grafo |
| `openharness` | MIT | Harness Python: permisos, swarm leader-worker, sandbox |
| `engram` | MIT | Memoria persistente + hooks de compactación y subagente |
| `hindsight` | MIT | Memoria con retain/recall/reflect y modelos mentales |
| `mem0` | Apache-2.0 | Memoria con historial por entrada |
| `LLMLingua` | MIT | Compresión de prompts; origen de SecurityLingua |
| `codegraph` | MIT | Contexto de código por grafo (FTS + relaciones) |
| `ruflo` | MIT | Orquestación swarm; mucho documento y poca API verificable |
| `grok-build` | Propia (SpaceXAI) | Evitar reutilización directa |
| `openhuman` | GPL-3.0 | Evitar reutilización directa (viral) |

La segunda ronda se completó el 2026-10-20. Los commits analizados están en la sección 12;
no se afirma nada de estos repos sin haber leído el archivo citado.

## 1. Contexto y compactación

### Hermes (MIT) — el compresor más maduro

Archivo: `hermes-agent/agent/context_compressor.py`

- Poda primero los resultados de tools viejos, sin LLM (barato).
- Protege cabeza (system + primer intercambio) y cola (presupuesto de tokens).
- Resume el medio con un modelo auxiliar barato.
- En compactaciones sucesivas **actualiza el resumen anterior** (`_previous_summary`),
  no resume desde cero.
- Presupuesto escalado: mínimo 2.000 tokens, 20% del contenido comprimido, techo 12.000
  (`_MIN_SUMMARY_TOKENS`, `_SUMMARY_RATIO`, `_SUMMARY_TOKENS_CEILING`).
- Cooldown de 600 s si el resumen falla (`_SUMMARY_FAILURE_COOLDOWN_SECONDS`), para no
  reintentar en cada turno.

### DeepSeek Harness (MIT) — compactación como transacción durable

Archivos: `packages/compaction/compaction/src/{index,types,invariant,tool-pairing,checkpoint}.ts`

- `compaction/start` → `compaction/summary` → `compaction/end`: lock durable, no dos
  compactaciones a la vez.
- El resumen es un nodo con procedencia (`checkpoint`), no una reescritura silenciosa.
- `toolPairingBalancedBefore/After` impide cortar un `tool_call` de su resultado.
- Si el proceso muere, el checkpoint permite recuperar; no queda un historial a medias.

### OpenCode (MIT) — resumen con identidad estable

Archivos: `internal/session/session.go`, `internal/llm/agent/agent.go`

- `Session.SummaryMessageID` apunta al mensaje que contiene el resumen.
- Al cargar la sesión, el historial se corta **desde ese mensaje** (`msgs[summaryMsgInex:]`),
  sin reescribir la lista completa.
- `Summarize()` crea un mensaje real en la sesión y guarda su ID; el resumen sobrevive
  a reinicios y se puede mostrar en la UI como “(summary)”.

### Continue (Apache-2.0) — resumen adjunto a un mensaje

Archivo: `continue/core/util/conversationCompaction.ts`

- `conversationSummary` se adjunta al mensaje hasta el que se compactó.
- Si se vuelve a compactar, reutiliza el resumen previo y solo incluye lo posterior.
- Evita duplicar resúmenes y conserva el punto de corte.

### Codex (Apache-2.0) — compactación asíncrona con control de versión

Archivos: `codex-rs/core/src/session/{async_compaction,context_window}.rs`,
`core/src/compact.rs`, `prompts/templates/compact/{prompt,summary_prefix}.md`

- Se evalúa por umbral de contexto en checkpoints y al final del turno
  (`turn_end_compaction_threshold_reached`), no solo cuando ya no entra el prompt.
- El resumen se pide en background y se adopta en un checkpoint; solo si
  `history_version` no cambió. Si el historial avanzó, el candidato se descarta
  (`async_compaction.rs:162-177`).
- Un request descartado igual cobra el uso ya consumido; el shutdown cancela el
  trabajo de red pendiente (`async_compaction.rs:1-2,186-205`).
- El resumen se inyecta con un preámbulo fijo (`summary_prefix.md`) que le dice al
  próximo modelo que hay trabajo previo y estado de tools, para no duplicarlo.
- `prompt.md` pide progreso, decisiones, restricciones, próximos pasos y datos críticos.

### OpenHands SDK (MIT) — condensación como evento durable

Archivos: `openhands-sdk/openhands/sdk/context/condenser/{pipeline_condenser,llm_summarizing_condenser}.py`,
`openhands-sdk/openhands/sdk/event/condenser.py`

- `PipelineCondenser` encadena condensadores y corta en cuanto uno devuelve
  `Condensation`, en vez de aplicar todos a ciegas.
- `Condensation` es un evento append-only con `forgotten_event_ids`, `summary` y
  `summary_offset`. El resumen se reinserta en la vista; no es un mensaje efímero.
- `LLMSummarizingCondenser` tiene `max_size=240`, `max_tokens`, `keep_first=2` y
  `minimum_progress=0.1`: si la condensación olvida menos del 10%, la trata como error.
  El hard reset reintenta hasta 5 veces reduciendo el tamaño por evento a 0,8.
- El historial mantiene árbol por `parent_id` (`event/base.py:33-38`); las ramas
  abandonadas quedan en disco y se pueden recuperar.

### Qué falta en `nexus-v3`

- `src/core/compaction.py` tiene 4 capas, pero el resumen L4 vive solo en la lista de
  mensajes en memoria. `SessionLog.compact_mark()` existe y **nadie lo llama**
  (`src/core/session_log.py:402`): escribe `sessions.summary`, pero el loop nunca lo
  invoca y no hay `summary_message_id` ni evento de compactación.
- `max_summary_tokens` es 800 (`compaction.py:115`), sin resumen iterativo, sin
  `minimum_progress` y sin cooldown.
- El corte `_snip` ya evita dejar un `tool` huérfano (bien), pero no tiene el contrato
  transaccional de DeepSeek ni el control de versión de Codex: si el historial cambia
  mientras se genera el resumen, nada impide insertar uno obsoleto.
- No hay preámbulo de handoff: el próximo turno no sabe que hubo compactación ni qué
  estado de tools quedó.

## 2. Permisos y aprobaciones

### OpenCode (MIT) — grant por sesión + tool + acción + path

Archivo: `internal/permission/permission.go`

- `PermissionRequest{SessionID, ToolName, Action, Path}`.
- `Grant` (one-shot) y `GrantPersistant` (sesión), con path normalizado al directorio.
- La tool queda **bloqueada** mientras se publica el pedido a la UI.
- Las tools MCP pasan por el mismo canal antes de ejecutarse.

### Hermes (MIT) — `once | session | always | deny`

Archivo: `hermes-agent/tools/approval.py`

- Estado por sesión (`contextvars`) y cola por sesión para approvals concurrentes.
- `always` persiste en `config.yaml`; `session` dura lo que la sesión; `deny` corta.
- Allowlist permanente de comandos y hook de aprobación para plugins.

### Goose (Apache-2.0) — permiso durable por hash de argumentos

Archivo: `goose/crates/goose/src/permission/permission_store.rs`

- Clave `tool_name + blake3(args/context)`; guarda `allowed`, `expiry`, timestamp.
- Persistencia atómica y limpieza de expirados.
- `permission_judge.rs` detecta tools de solo lectura para no pedir permiso de más.
- `security/` tiene inspectores de egress y de comportamiento adversario.

### DeepSeek Harness (MIT) — escalado de sandbox fail-closed

Archivo: `packages/sandbox/sandbox/src/escalation.ts`

- Solo se puede escalar a un modo **estrictamente más amplio**.
- La aprobación ocurre **antes** de ejecutar; sin aprobación, no hay ejecución.
- Distingue denegación de fallo del runner.

### Codex (Apache-2.0) — decisión tipada, cache por sesión y política por prefijo

Archivos: `codex-rs/protocol/src/protocol.rs` (`ReviewDecision`, `AskForApproval`),
`core/src/tools/sandboxing.rs` (`with_cached_approval`),
`core/src/tools/runtimes/unified_exec.rs` (`UnifiedExecApprovalKey`),
`execpolicy/README.md`, `hooks/src/events/permission_request.rs`

- `ReviewDecision` no es un booleano: `Approved`, `ApprovedExecpolicyAmendment`,
  `ApprovedForSession`, `ApprovedMcpPolicyAmendment`, `NetworkPolicyAmendment`,
  `Denied{rejection}`, `TimedOut`, `Abort`. El `Default` es `Denied`.
- `with_cached_approval` solo evita el prompt si **todas** las claves están
  `ApprovedForSession`; con claves vacías nunca cachea; solo guarda cuando la
  decisión nueva es `ApprovedForSession` (`sandboxing.rs`).
- La clave de `unified_exec` incluye `environment_id`, `executable`, `command[]`,
  `cwd`, `tty`, `sandbox_permissions` y `additional_permissions`. Es más fina que
  “tool + path”: el mismo binario en otro cwd vuelve a pedir aprobación.
- `execpolicy` usa reglas de prefijo con `decision = allow | prompt | forbidden`,
  `justification`, y `match`/`not_match` como tests que se validan al cargar. Ojo: si
  se omite `decision`, el default es `allow`; para `nexus-v3` conviene el default
  inverso (deny) y exigir reglas explícitas.
- `AskForApproval::Granular` permite apagar por categoría (`sandbox_approval`,
  `rules`, `skill_approval`, `request_permissions`, `mcp_elicitations`); cuando el
  flag es `false`, el pedido se **rechaza automáticamente** en vez de mostrarse.
- Los hooks `PermissionRequest` pueden devolver allow/deny; la decisión se pliega
  con precedencia de deny. `interrupt: true` y `updatedInput` hoy fallan cerrado.
- Los subagentes (`codex_delegate.rs:50,67-76`) exigen `approval_policy = never` y
  heredan el `exec_policy` del padre: un hijo no puede escalar a aprobación humana.

### OpenHands SDK (MIT) — riesgo por umbral + guardrail separado

Archivos: `openhands-sdk/openhands/sdk/security/{confirmation_policy,toolshield_llm_analyzer}.py`,
`agent/agent.py` (`_analyze_for_confirmation`, `_set_confirmation_status`)

- `ConfirmRisky(threshold=HIGH, confirm_unknown=True)`: `UNKNOWN` se confirma por
  defecto. `NeverConfirm` y `AlwaysConfirm` son políticas explícitas, no un flag suelto.
- El actor puede anotar `security_risk`, pero el guardrail (`ToolShieldLLMSecurityAnalyzer`)
  hace **otra** completion con un LLM distinto y evalúa la secuencia completa
  (ventana de 20 acciones), no la acción aislada. Eso detecta ataques multi-turno
  donde cada paso parece benigno.
- Renderiza tool/summary/thought/arguments en tags XML escapados y prohíbe adoptar
  un `RISK:` que venga dentro del contenido: el veredicto se emite fuera del span.
- Si el riesgo supera el umbral, el estado pasa a `WAITING_FOR_CONFIRMATION` y la
  acción no se ejecuta hasta la segunda llamada (`local_conversation.py:1949-1956`).

### Qué falta en `nexus-v3`

- `scope.py` tiene deny duro de secretos y modos `read_only|workspace_write|full`, pero
  `config/config.yaml` arranca con `permission: full` y
  `require_confirm_mutating: false`.
- `ApprovalGate` (`src/core/approval_gate.py`) no se instancia en `src/api/server.py`;
  solo aparece en `tests/test_plan_mode.py` y en `plan_mode.py`. Sus `GATED_ACTIONS`
  (`git push --force`, `pr_merge`, `release`, etc.) **no se consultan** antes de
  despachar una tool (`is_gated_action` no tiene llamadores en `src/`).
- `Approvals` (`src/core/approvals.py`) es una cola en memoria y solo la usan los
  endpoints de archivos (`/api/files/write`, `/api/files/edit`), no el loop del agente.
- Las tools MCP se registran como `category="mcp"` (`tool_registry.py:237`) pero sus
  nombres arbitrarios no están en `MUTATING_TOOLS`; no hay aprobación MCP.
- No hay precedencia de deny: un “always” persistido podría vencer una regla estática
  porque no existe una evaluación separada de reglas vs. grants. MiMo lo resuelve
  evaluando el ruleset solo primero (`packages/opencode/src/permission/index.ts:186-196`).
- En modo no interactivo no hay política explícita: si el pedido de aprobación no
  puede mostrarse, el comportamiento correcto es **fallar cerrado**, como
  `AskForApproval::Granular` de Codex cuando el flag es `false`.

Matiz: `hooks_engine` puede bloquear una tool si un handler devuelve `STOP`/`COMPACT`
(`tool_registry.py:380-390`), pero no hay un handler de seguridad registrado por defecto;
`security_gate` solo aparece en `stats()`. El gate humano real faltaba.

Esto era el gap más importante para compartir el proyecto: el agente podía ejecutar
comandos destructivos sin un gate humano, aunque los secretos duros siguieran bloqueados.

**Estado al 2026-10-20 (cierre P0).** Los seis gaps de arriba quedaron cerrados y
verificados:

- Gate real en `ToolRegistry.dispatch` (`src/core/tool_registry.py`): recibe
  `approval`/`session_id` y llama a `src/core/approval_policy.authorize` después del
  scope guard y antes de los hooks. Es el choke point que usan el loop, los subagentes
  y el servidor MCP entrante.
- Orden fail-closed: hard deny → deny grant → allow grant → política
  (`auto|assisted|strict|never`) → pedido humano. Sin canal interactivo se deniega en
  el acto; no se bloquea el proceso.
- Grants durables en SQLite (`data/approvals.db`, WAL): `once` se consume, `session`
  dura la sesión, `always` es global; un `deny` gana sobre cualquier `allow`; clave
  vacía nunca cachea. Endpoints nuevos: `GET /api/approvals/grants`,
  `POST /api/approvals/grants/clear`; `approve|reject` aceptan `{"scope": ...}`.
- MCP: toda llamada `mcp_call` pide aprobación salvo allowlist de solo lectura por
  `server/tool` (`approval.mcp_read_only`, con wildcards). Un “siempre” MCP se guarda
  por `server+tool`, no por argumentos.
- Subagentes, gemas y servidor MCP entrante corren con `interactive=False` y
  `policy="never"`: fail-closed. El subagente además hereda los `deny` del padre
  (`parent_session_id`); los `allow` del padre no se heredan.
- `write_file`/`edit` conservan su gate en el handler cuando `auto_apply=approve`; si
  no hay UI, ahora lanzan `PermissionError` en vez de encolar un pedido que nadie
  responderá.

Evidencia: `tests/test_approvals_gate.py` (8 tests: no interactivo, allowlist MCP,
“siempre” MCP por server+tool, `apply_patch`, grant de sesión, `once` consumido,
herencia de deny del padre y `write_file` fail-closed). El caso real
`git push --force origin main` generó dos pedidos y ambos expiraron sin ejecutar el
comando (`approvals` con `status=timed_out`).

## 3. Subagentes

### DeepSeek Harness (MIT)

- `packages/subagent/subagent/src/depth.ts`: profundidad persistida y monotónica.
- `lifecycle.ts`: activación, observers y continuidad; el hijo no puede “olvidar” su
  profundidad al reanudar.
- `continuation.ts`: el subagente es continuable, no solo one-shot.

### OpenCode (MIT)

- `internal/llm/agent/agent-tool.go`: cada invocación crea una sesión hija; el costo del
  hijo se suma al padre.

### Codex (Apache-2.0) — el subagente no puede escalar permisos

Archivo: `codex-rs/core/src/codex_delegate.rs`

- El delegate exige `approval_policy = never`; si el padre no la tiene, falla con
  “Codex delegates require approval policy `never`” (`:67-76`).
- Hereda el `exec_policy` del padre (`:151`): las reglas `forbidden` siguen vigentes
  dentro del hijo.
- No recibe un canal de aprobación: si algo no está permitido, el hijo falla y el
  error vuelve al padre. Es la política correcta para subagentes desatendidos.

### `nexus-v3`

Ya tiene lo importante: `MAX_DEPTH=3`, sesión hija, `allowed_tools` y modos
`one-shot|continuable` (`src/core/sub_agent_spawner.py`). No hace falta copiar el modelo
de DeepSeek; sí conviene revisar que la profundidad se persista al reanudar en frío.

## 4. Terminal, procesos y sandbox

### Hermes (MIT)

`tools/process_registry.py`: PTY real, background, stdin interactivo, wait/kill, lectura
por streaming. Es el patrón para programas de terminal (vim, top, REPL).

### DeepSeek Harness (MIT)

`packages/terminal/terminal-bash/src/session.ts`: PTY persistente con `@xterm/headless`
y buffers acotados; no acumula salida sin límite.

### `nexus-v3`

Hay `src/tools/persistent_shell.py` con stdin y proceso persistente, y jobs en background,
pero **no hay PTY**. Programas que detectan TTY no funcionan bien. Es un gap de
conveniencia, no de seguridad; se puede resolver con `pywinpty` opcional y fallback al
shell actual.

## 5. Sandbox y seguridad de ejecución

### Goose (Apache-2.0)

`security/egress_inspector.rs`, `adversary_inspector.rs`, `scanner.rs`: inspección de
salida de red y de comportamiento antes de ejecutar. Útil como lista de patrones, no
como dependencia directa.

### DeepSeek Harness (MIT)

`packages/sandbox/sandbox/src/roots.ts` + `escalation.ts`: modos de sandbox, roots
explícitos y escalado aprobado. `src/core/sandbox_service.py` de nexus ya tiene allowlist
de entorno y kill del árbol; falta la escalera de modos y la aprobación previa.

### Codex (Apache-2.0)

Archivos: `codex-rs/protocol/src/config_types.rs` (`SandboxPolicy`),
`protocol/src/protocol.rs` (`AskForApproval`), `execpolicy/README.md`

- Tres modos: `read-only`, `workspace-write`, `danger-full-access`; el sandbox externo
  es un cuarto caso (`ExternalSandbox`).
- `AskForApproval`: `UnlessTrusted` (proyecto no confiable), `OnRequest` (default),
  `Granular` (por categoría) y `Never`.
- `execpolicy` separa la decisión de política (`allow/prompt/forbidden`) de la
  decisión del usuario; una regla `forbidden` no se puede aprobar en el momento.
- Para `nexus-v3`, el equivalente es `scope.permission` + `is_gated_action` + una
  política de “sin UI ⇒ deny”, no un único booleano `full`.

## 6. Inteligencia de código

### Aider (Apache-2.0)

`aider/repomap.py`: tree-sitter + PageRank, personalización por archivos/identificadores
mencionados y ajuste al presupuesto de tokens con búsqueda binaria. Es la mejor referencia
para un “repo map” que no explote el contexto.

### OpenCode (MIT)

Diagnósticos LSP inyectados en el resultado de `edit/write/view`, con espera acotada.
`nexus-v3` ya tiene `lsp_client.py` y diagnóstico post-edit; lo que falta es el repo-map
con ranking, no el LSP.

### CodeGraph (MIT)

`src/context/index.ts`: construye un “task context” con límites explícitos de nodos,
bloques de código y profundidad, en vez de volcar el grafo entero. Es el complemento
correcto al PageRank de Aider: Aider elige qué archivos, CodeGraph acota qué nodos y
relaciones se muestran. `nexus-v3` ya tiene grafo (`knowledge_graph.db`) y
`codebase_context.py`; falta el presupuesto por consulta.

## 7. Event store, memoria y UI

### OpenHands app/server (MIT, excepto enterprise)

Este snapshot es la app/server v1 (`openhands/app_server`, `openhands/server`), sin el
core del agente. Su valor real está en `app_server/event_callback/*`
(callbacks/webhooks por evento), el event store y la UI.

### OpenHands SDK (MIT) — log append-only con árbol de ramas

Archivos: `openhands-sdk/openhands/sdk/event/base.py`,
`openhands-sdk/openhands/sdk/conversation/event_store.py`

- Cada evento tiene `id` (minteado en proceso) y `parent_id`; los eventos con el mismo
  `parent_id` son ramas hermanas.
- El log es append-only: navegar a un evento anterior y volver a escribir crea un
  hermano; los eventos abandonados quedan en disco pero salen de `state.view`
  (`local_conversation.py:963`).
- Esto es más fuerte que un `fork` de sesión: el historial completo se conserva y se
  puede reconstruir `path_to_root(event_id)`.

### Engram (MIT) — recuperación después de compactar

Archivos: `plugin/claude-code/scripts/{post-compaction,session-start,subagent-stop}.sh`,
`docs/codebase/memory-core.md`

- `post-compaction.sh` inyecta el “Memory Protocol” y el contexto del proyecto, y le
  pide al agente persistir el resumen con `mem_session_summary`.
- `session-start.sh` recupera el estado al abrir; `subagent-stop.sh` consolida lo que
  hizo el subagente.
- Entidades separadas: `sessions`, `observations`, `user_prompts`, `memory_relations`,
  `sync_mutations`. Búsqueda progresiva: primero resultados compactos y
  `mem_get_observation` solo cuando hace falta el contenido completo.
- Lección para `nexus-v3`: compactar no alcanza; hay que **reinyectar** el resumen y
  dejar un gancho para que el subagente cierre su propia memoria.

### Hindsight (MIT) — retain asíncrono y recall con presupuesto

Archivos: `hindsight-api-slim/hindsight_api/mcp_tools.py`,
`hindsight-api-slim/hindsight_api/engine/memory_engine.py`

- `retain` acepta `context`, `tags`, `document_id`, `bank_id`, `strategy` y
  `update_mode`, y usa `submit_async_retain` (`mcp_tools.py:1044,1103`): la escritura
  no bloquea el turno.
- `recall` tiene `budget: low|mid|high`, `max_tokens` (default 4096) y
  `prefer_observations`, que descarta hechos crudos ya consolidados en una observación
  para no duplicar contexto.
- `reflect` y los “mental models” sintetizan estado de alto nivel en vez de devolver
  todos los hechos. Es el patrón para un `brain_recall` con presupuesto, no para
  inyectar la base entera.

### DeepSeek Harness (MIT)

Invariante “model-visible ⟺ logged”; proyecciones incrementales del log. `nexus-v3` ya
tiene `event_store.py`; el siguiente paso sería derivar mensajes/proyecciones del log en
vez de mantener listas paralelas.

### jcode (MIT) — memoria asíncrona en grafo

Archivos: `docs/MEMORY_ARCHITECTURE.md`, `src/memory.rs`

- El agente principal nunca espera a la memoria: lo del turno N está disponible en N+1.
- Los recuerdos forman un grafo con tags, clusters y links semánticos; un hit de
  embedding dispara BFS para traer relacionados.
- Límites concretos: 30 candidatos → 10 resultados (`MEMORY_RELEVANCE_MAX_CANDIDATES`,
  `MEMORY_RELEVANCE_MAX_RESULTS`), poda por gap (factor 0,25, mínimo 1) y dedupe de
  almacenamiento a 0,85 (`STORAGE_DEDUP_THRESHOLD`).
- Es el complemento de Hindsight: Hindsight prioriza presupuesto de recall; jcode
  prioriza no bloquear el loop.

### mem0 (Apache-2.0) — historial por memoria

`mem0/client/main.py` (`history(memory_id)`) y `mem0-ts/.../MemoryHistoryManager.ts`:
cada memoria tiene historial de cambios, no solo el valor actual. Para `nexus-v3`
implica que `brain_remember`/`memory_set` deberían dejar auditoría de quién cambió qué,
en vez de sobrescribir sin traza.

## 8. Claude Code y Codex: límites concretos

- **Claude Code**: el repo local `D:\ias\autopsia\claude-code` no contiene el CLI. Solo
  trae `.claude/`, `plugins/hookify`, `plugins/security-guidance`, ejemplos y docs.
  `LICENSE.md` dice “© Anthropic PBC. All rights reserved”. El binario instalado
  (`@anthropic-ai/claude-code/bin/claude.exe`, ~254 MB) es propietario. Se puede estudiar
  su comportamiento y el protocolo de hooks, **no copiar código**.
- **Codex**: el paquete instalado `@openai/codex@0.160.1` era solo un wrapper
  (`bin/codex.js`) que descarga un binario. El upstream ya está clonado en
  `D:\ias\autopsia\codex` (commit `66d0de9`, 2026-10-20, Apache-2.0, 9.379 archivos).
  `codex-rs/` es la fuente real del agente. Es reutilizable con aviso de licencia, pero
  está muy acoplado a su protocolo (`Session`, `TurnContext`, `StepContext`): conviene
  copiar el **diseño** (decisiones tipadas, cache por clave, execpolicy, delegates sin
  aprobación), no el código.

## 8.1 Otras referencias puntuales

### MiMo (MIT) — precedencia de deny y auto-accept por linaje

Archivos: `packages/opencode/src/permission/{evaluate,index}.ts`,
`packages/app/src/context/permission-auto-respond.ts`

- `evaluate()` devuelve la **última** regla que matchea; si no hay, `ask`.
- `index.ts:186-196` evalúa el ruleset solo primero: un `deny` explícito gana aunque
  exista un “always” persistido. Solo si el ruleset no deniega se permite que una
  aprobación convierta `ask` en `allow`.
- El auto-accept se guarda por sesión y por directorio, y se hereda por linaje de
  sesiones padre (`sessionLineage`). Es una forma de grants por árbol de subagentes,
  no un booleano global.
- Plan mode: `docs/compose/reports/plan-mode-edit-write-backstop.md` documenta que el
  permiso es un **backstop**, no un gate activo; rutear `bash` a `ask` generaba fatiga
  de prompts. Para `nexus-v3`, esto respalda separar “bloquear escritura en plan mode”
  de “pedir aprobación por cada comando de lectura”.

### OpenHarness (MIT) — credenciales antes que allowlist

Archivos: `src/openharness/permissions/checker.py`,
`src/openharness/swarm/permission_sync.py`

- `SENSITIVE_PATH_PATTERNS` (`.ssh`, `.aws`, `.gnupg`, `.kube`, `.docker`, etc.) se
  evalúa **antes** de `allowed_tools`, `denied_tools` y del modo. Ningún modo puede
  habilitar esas rutas.
- `denied_tools` y `denied_commands` se evalúan antes de `FULL_AUTO`; plan mode bloquea
  mutantes; el modo default pide confirmación.
- `permission_sync.py` implementa pedidos de permiso leader-worker por archivos
  `pending/<id>.json` → `resolved/<id>.json` o por mailbox, con IDs y polling. Es el
  patrón para aprobaciones de subagentes cuando el padre es el único que tiene UI.

### LLMLingua (MIT)

`llmlingua/prompt_compressor.py`: compresión de prompts por presupuesto, con una
variante SecurityLingua. `nexus-v3` ya nombra una capa “SecurityLingua” en su pipeline
de compactación; este repo es la referencia upstream, no una dependencia nueva.

### Ruflo (MIT)

`plugin/`, `plugins/`, `.agents/skills/{swarm-orchestration,memory-management}`: catálogo
de patrones de swarm y consolidación de memoria. Mucho es documentación/definiciones de
agentes, no una API runtime compacta; sirve como checklist, no como dependencia.

## 8.2 Harness² (Apache-2.0): metodología, no runtime

Repo: `github.com/google-research/harness2`, commit `1cfe254` (2026-10-13).
Es un framework de **investigación** para mejorar el harness de forma recursiva: separa
explícitamente tres ejes que en `nexus-v3` están mezclados — **método** (recursión,
prompts, selección, biblioteca), **substrate** (el agente que ejecuta) y **benchmark**
(tareas + scorer). No es un agente ni un drop-in.

### Qué mecanismo aporta

Archivos: `harness2/core/{spec,tree,recursion,ledger,library}.py`,
`harness2/substrate/{base,opencode,codex}.py`.

- **Taxonomía c1..c8** (`tree.py:15`, `opencode.py:42-84`): ocho componentes editables —
  system prompt, guardrails, memoria de largo plazo, tool guidance, skills, subagentes,
  scripts y plugins. Cada uno con path, regex de validación y reglas de frontmatter. En
  `nexus-v3` el equivalente está repartido entre `config/config.yaml`, `AGENTS.md`,
  `src/core/hooks.py`, `src/tools/builtin.py`, `brain/` y `src/core/sub_agent_spawner.py`,
  sin un contrato único de lectura/validación/escritura.
- **Contrato de edición “omitted-is-inherited”** (`tree.py`): una propuesta emite solo lo
  que cambia; el validador reconstruye el árbol y rechaza propuestas que rompen
  referencias (skill sin trigger, subagente sin frontmatter, script reservado). Es el
  patrón que le falta al self-learning de `nexus-v3`, que hoy escribe observaciones sin
  validar el estado resultante.
- **Propose–Probe–Compose** (`recursion.py`): el proposer no ve scores ni al grader; se
  generan variantes en paralelo, se prueban contra el benchmark y se componen las que
  ganan, con un reintento y fallback al árbol base si la propuesta es inválida. La
  separación proposer/grader evita el reward hacking que sí es posible cuando el mismo
  loop se autoevalúa.
- **Ledger de presupuesto** (`ledger.py:15`): identidad `R = 4k` rollouts y `P = 3k`
  etapas de mejora, con costo en USD y verificación de que el ledger “cierra”. Es
  contabilidad de experimentos, no de sesión.
- **Biblioteca de experiencias por dominio** (`library.py`): entradas
  `cue / lesson / evidence / confidence`, persistentes entre tareas. Es más cercano a lo
  que `nexus-v3` necesita para el grafo de conocimiento que una tabla de observaciones
  sueltas.

### Por qué no se adopta como dependencia

- **POSIX-only**: `pyproject.toml:11,15` declara `requires-python >=3.12,<3.14` y
  `Operating System :: POSIX`. Usa `os.killpg`, `signal.SIGKILL`, `bash`, `/bin`,
  `.venv/bin/python`, `python3`, symlinks y `chmod`. `nexus-v3` corre en Windows con
  `.venv-ft314` (3.14): no hay soporte ni ejecución nativa.
- **El demo solo corre en Linux**: lo ejecuté en el WSL2/Ubuntu de esta PC
  (`uv` + Python 3.12) y termina con `2 tasks completed; 0 failed` (28 ejecuciones,
  32 llamadas al editor). Son dos tareas sintéticas de contabilidad: valida el
  plumbing, no capacidad real. En Windows nativo no hay forma de levantarlo sin
  reescribir el substrate.
- **No habla con `nexus-v3`**: los substrates son OpenCode y Codex (`substrate/base.py`
  define `surface/materialise/execute/read_trajectory/preflight`). Usarlo exigiría un
  `NexusSubstrate` y un `BenchmarkAdapter` propios; eso es un proyecto, no un parche.
- **No resuelve ninguno de los seis P0**: no trae approval gate, grants durables,
  aprobación MCP, precedencia de deny, política no interactiva ni resumen durable. Su
  propio ejecutor Codex corre con `--dangerously-bypass-approvals-and-sandbox` y
  `--dangerously-bypass-hook-trust` (`substrate/codex.py:166-167`) y red sin restricción:
  **no es referencia de seguridad**.

### Qué sí conviene copiar (adaptado, con aviso Apache-2.0)

1. Un `HarnessSurface` en `nexus-v3` que declare los componentes editables con path,
   schema y validador, y exponga `read/validate/diff/write`.
2. El contrato “omitted-is-inherited” + un reintento + fallback al estado base para
   cualquier edición automática de prompts, skills o tool guidance.
3. Un ledger de evidencia por cambio de harness: propuesta, probe, resultado, costo y
   decisión (`adoptada | revertida`), separado del log de sesión.
4. Una biblioteca por dominio con `cue/lesson/evidence/confidence`, enlazada al grafo de
   conocimiento existente en vez de crear otro store.
5. La separación proposer/grader: el que propone no ve el scorer. Es barato de implementar
   y corrige un riesgo real del self-learning actual.

Orden correcto: primero los seis P0 del §10; después este surface. Al revés se automatiza
la edición de un harness que todavía puede ejecutar cualquier comando sin aprobación.

## 9. Matriz de gaps de `nexus-v3` y prioridad

| # | Gap | Evidencia | Patrón a adoptar | Prioridad |
|---|---|---|---|---|
| 1 | Aprobación no cableada al despacho de tools | `agent_loop.py:888/948` despacha directo; `is_gated_action()` sin llamadores en `src/` | Codex `ReviewDecision` + gate antes de `dispatch` | P0 |
| 2 | Grants en memoria y a nivel tarea | `approvals.py`; `approval_gate.py`; no hay tabla de grants | Codex `UnifiedExecApprovalKey` + `ApprovedForSession`; OpenCode grant persistente | P0 |
| 3 | MCP sin aprobación | `tool_registry.py:237`; `scope.MUTATING_TOOLS` no cubre nombres MCP | Codex `ApprovedMcpPolicyAmendment`; aprobar server+tool+args | P0 |
| 4 | Un “always” puede vencer un deny estático | no hay evaluación separada ruleset vs. grants | MiMo `index.ts:186-196`: ruleset solo primero, deny gana | P0 |
| 5 | Sin política no interactiva | no hay rama “no hay UI ⇒ deny” | Codex `Granular` (flag `false` ⇒ rechazo automático) | P0 |
| 6 | Subagente sin política de aprobación | `sub_agent_spawner.py` estrecha scope pero no fija approval policy | Codex delegates: `approval_policy=never` + heredar `exec_policy` | P0/P1 |
| 7 | Resumen de compactación efímero | `session_log.compact_mark()` sin llamadas; no hay `summary_message_id` | OpenHands `Condensation` event / OpenCode `SummaryMessageID` | P1 |
| 8 | Resumen chico y sin progreso mínimo | `compaction.py:115,224` (800 tokens) | Hermes 2K/20%/12K + OpenHands `minimum_progress` | P1 |
| 9 | Sin reinyección post-compactación | no hay hook `on_compact` que reinjecte resumen | Engram `post-compaction.sh` + `mem_session_summary` | P1 |
| 10 | Sin guardrail de secuencia | el riesgo se evalúa por tool, no por secuencia | OpenHands ToolShield (ventana de 20 acciones) | P1 |
| 11 | Sin PTY | `persistent_shell.py` usa pipes | Hermes `process_registry` / DeepSeek `terminal-bash` | P2 |
| 12 | Sin repo-map rankeado | `codebase_context.py` no usa PageRank | Aider `repomap.py` + CodeGraph límites por consulta | P2 |
| 13 | Anti-loop con umbrales fijos | `agent_loop.py:923-935` (2/3/4) | DeepSeek: umbrales 3/5/8 configurables y canonicalización | P2 |
| 14 | Sandbox sin escalera de modos | `sandbox_service.py` | Codex `SandboxPolicy` / DeepSeek `escalation.ts` | P2 |
| 15 | Memoria sin presupuesto ni async | `brain_recall` sin `max_tokens`/budget | Hindsight `recall(budget, max_tokens)` + jcode async | P2 |

## 10. Plan de parches recomendado

### P0 — antes de compartir como agente autónomo

**Estado 2026-10-20: implementado y verificado.** Tabla de cierre:

| P0 | Estado | Dónde |
|---|---|---|
| 1. Gate en `dispatch` | ✅ | `tool_registry.py` + `approval_policy.py` |
| 2. Decisión tipada / fail-closed | ✅ | `ApprovalOutcome` (`allow|denied|timed_out`) |
| 3. Grants durables | ✅ | `approvals.py` (`GrantStore`, SQLite WAL) |
| 4. MCP con aprobación | ✅ | `mcp_call` + `approval.mcp_read_only` |
| 5. Subagentes sin canal | ✅ | `policy="never"`, `interactive=False`, deny del padre |
| 6. Resumen durable | ✅ | `sessions.summary_message_id` + mensaje `system` |

Verificación: `pytest -q` → 648 collected, 645 passed, 3 skipped, exit 0;
`scripts/e2e_diagnostic.py` → `total=41 pass=41 fail=0`; backend :8765 reiniciado y
`GET /api/approvals/grants` responde 200.

1. **Gate en `ToolRegistry.dispatch`, no solo en `agent_loop`**:
   - `dispatch` es el único choke point: lo usan el loop, los subagentes y los endpoints.
     Firmar `dispatch(..., approval=None, session_id=None)` y resolver ahí.
   - Antes de ejecutar, evaluar en este orden: (a) deny duro de `scope` y credenciales;
     (b) reglas estáticas (`is_gated_action` + patrones de comando); (c) grants
     durables; (d) pedido humano. Un deny de (a)/(b) no se puede vencer con un grant.
   - Si la tool es `exec/terminal`, clasificar el comando completo, no el nombre de la
     tool. Si es MCP, clasificar `server + tool + args`.
2. **Decisión tipada y fail-closed**:
   - Reemplazar el booleano por `approved | approved_for_session | approved_always |
     denied | timed_out | abort`, con `denied` como default.
   - `ApprovalGate.request_approval(..., escalation="reject")` y
     `wait_for_approval`; nunca `auto_approve` para una tool. Timeout ⇒ `denied`.
   - Si no hay UI/canal interactivo, no llamar a `wait_for_approval`: devolver
     `denied` inmediatamente (Codex `Granular` con flag `false`).
   - El servidor MCP entrante (`src/mcp_server.py:54`) también despacha tools y no
     tiene UI: ahí la política debe ser allowlist explícita o deny, nunca “preguntar”.
3. **Grants durables**:
   - Tabla `approval_grants(session_id, tool, action, key_hash, decision, expires_at)`.
   - Clave mínima: `tool + args_normalizados + cwd + permission_mode`; para exec,
     `command[]` completo (Codex `UnifiedExecApprovalKey`). Clave vacía nunca cachea.
   - `once` se consume; `session` dura la sesión; `always` no tiene `session_id`;
     `deny` se guarda y gana sobre cualquier `allow`.
   - Mantener `Approvals` en memoria solo como cache de la cola, no como fuente de verdad.
4. **MCP con aprobación explícita**:
   - Toda tool `category="mcp"` pide aprobación salvo allowlist de solo lectura por
     `server + tool`.
   - Persistir la aprobación como `ApprovedMcpPolicyAmendment` (Codex): un “siempre”
     por server+tool, no por sesión de chat.
5. **Subagentes sin canal de aprobación**:
   - `sub_agent_spawner` ya estrecha `scope_roots`, pero debe además fijar
     `approval_policy=never` y heredar el deny set del padre, como `codex_delegate.rs`.
     Si el hijo necesita algo prohibido, falla y el error vuelve; no abre un prompt nuevo.
6. **Resumen durable**:
   - Migración `ALTER TABLE sessions ADD COLUMN summary_message_id INTEGER`.
   - Al compactar, insertar el resumen como mensaje real y guardar su ID; al cargar,
     cortar el historial desde ese mensaje (OpenCode).
   - Alternativa equivalente: un evento `Condensation` append-only con `summary` y
     `summary_offset` (OpenHands SDK).

### P1 — calidad de contexto

7. Subir el presupuesto del resumen a mínimo 2.000 tokens, 20% del contenido, techo 12.000.
8. Reutilizar el resumen previo, agregar `minimum_progress` y cooldown de 600 s tras fallo.
9. Reinyectar el resumen después de compactar con un preámbulo de handoff (Codex
   `summary_prefix.md`) y un hook `on_compact` (Engram).
10. Guardrail de secuencia: un LLM separado evalúa las últimas 20 acciones y etiqueta
    `LOW/MEDIUM/HIGH`; el actor no decide su propio riesgo (OpenHands ToolShield).
11. Podar resultados de tools antes de llamar al LLM (Hermes).

### P2 — conveniencia

12. PTY opcional (`pywinpty`) con fallback a `persistent_shell`.
13. Repo-map con tree-sitter + PageRank y presupuesto binario.
14. Umbrales de anti-loop configurables (`3/5/8`).
15. Escalera de sandbox (`read-only | workspace-write | danger-full-access`) con
    escalado solo hacia arriba y aprobación previa.
16. `brain_recall` con `budget`/`max_tokens` y escritura asíncrona (Hindsight/jcode).

## 11. Veredicto

### Evidencia de verificación (2026-10-20)

| Prueba | Resultado |
|---|---|
| `npx tsc --noEmit` en `ui/` | exit 0, sin errores |
| `npm run build` en `ui/` | 2.834 módulos, 6,70 s, `dist/` generado; aviso no fatal de chunks >700 kB (`three.module`, `react-force-graph-3d`) |
| `scripts/e2e_diagnostic.py` | `total=41 pass=41 fail=0` |
| Suite `pytest -q` | **648 collected, 645 passed, 0 failures, 0 errors, 3 skipped**, exit 0; `test_ui_build.py` exit 0 |
| Tests P0 nuevos | `tests/test_approvals_gate.py` (8) + `tests/test_durable_memory.py` (5) |
| Gate real (smoke) | `git push --force origin main` → 2 pedidos, ambos `timed_out`, comando no ejecutado |
| Endpoints nuevos | `GET /api/approvals/grants` 200; `POST /api/approvals/grants/clear` 200; backend :8765 reiniciado |
| `gitleaks git .` | 80 commits, 5,72 MB escaneados, **no leaks found** |
| Tamaño del repo | 5,78 MiB de objetos; archivo tracked más grande: `docs/fase7-ui-sesiones.png` (0,51 MB) |
| Corrección aplicada | `tests/test_ui_build.py`: usa `node node_modules/typescript/bin/tsc` (fallback `npx --no-install`), timeout 240 s; build 300 s |

El fallo anterior de `test_tsc_noemit` era solo `TimeoutExpired` a 60 s: `tsc` tarda más
en frío en esta PC. No había errores de tipos.

### Evidencia de análisis (segunda ronda)

| Repo | Commit | Licencia | Qué se leyó |
|---|---|---|---|
| `codex` | `66d0de9` (2026-10-20) | Apache-2.0 | `ReviewDecision`, `with_cached_approval`, `execpolicy`, `PermissionRequest`, `async_compaction`, `codex_delegate` |
| `openhands-sdk` | `0996c33` (2026-10-20) | MIT | `pipeline_condenser`, `llm_summarizing_condenser`, `Condensation`, `confirmation_policy`, `toolshield_llm_analyzer`, event store |
| `mimo-code` | `1c5217e` (2026-06-30) | MIT | `permission/{evaluate,index}.ts`, `permission-auto-respond.ts`, plan-mode backstop |
| `jcode` | `020c782d` (2026-05-23) | MIT | `docs/MEMORY_ARCHITECTURE.md`, `src/memory.rs` |
| `openharness` | `033dce6` (2026-05-24) | MIT | `permissions/checker.py`, `swarm/permission_sync.py` |
| `engram` | `2f8e5e8` (2026-05-18) | MIT | `post-compaction.sh`, `session-start.sh`, `subagent-stop.sh`, `memory-core.md` |
| `hindsight` | `eb24aa8` (2026-10-09) | MIT | `retain`/`recall` en `mcp_tools.py`, `Budget`, `submit_async_retain` |
| `mem0` | `edd1b3e` (2026-05-20) | Apache-2.0 | `history(memory_id)`, `MemoryHistoryManager` |
| `LLMLingua` | `e0e9d99` (2025-10-28) | MIT | `llmlingua/prompt_compressor.py` |
| `codegraph` | `9b6a917` (2026-05-18) | MIT | `src/context/index.ts` |
| `ruflo` | `fa790f09` (2026-05-20) | MIT | `plugin/`, `.agents/skills/{swarm-orchestration,memory-management}` |
| `harness2` | `1cfe254` (2026-10-13) | Apache-2.0 | `core/{spec,tree,recursion,ledger,library}.py`, `substrate/{base,opencode,codex}.py`, `pyproject.toml` |

Prueba de ejecución de Harness²: en WSL2/Ubuntu, `uv venv --python 3.12`,
`uv pip install -e '.[render]'`, `harness2-demo --output runs/demo --k 2` →
`2 tasks completed; 0 failed`; `demo.json` registra 28 ejecuciones y 32 llamadas al
editor sobre dos tareas sintéticas de contabilidad. En Windows nativo no aplica:
`pyproject.toml` declara POSIX y el código usa `os.killpg`, `bash` y rutas `/bin`.

Límites de esta evidencia:

- Es análisis de código y diseño, no un benchmark de esos harnesses.
- No se copió código de `claude-code` (propietario), `grok-build` ni `openhuman`.
- Los clones son `--depth 1`; los commits son la foto del 2026-10-20, no un seguimiento.
- La primera pasada (análisis) no modificó código de `nexus-v3`; la segunda pasada
  (P0, misma fecha) implementó gate, grants durables, resumen durable, aristas tipadas
  y rerank semántico.

### Conclusión

- **Como repo de código**: está listo para compartirse. Build, tipos, E2E y suite pasan.
- **Como agente para uso desatendido**: mucho más cerca, pero no “sin ninguna
  supervisión”. El gate real ya está cableado en `ToolRegistry.dispatch`, con precedencia
  de deny, grants durables por clave, aprobación MCP, fail-closed sin UI y subagentes sin
  escalada. El default sigue siendo `scope.permission: full` y `approval.mode: assisted`
  (solo las acciones sensibles piden aprobación); para uso 100% desatendido conviene
  `approval.mode: strict` o `never` y acotar `scope.allowed_roots`.
- **P0 cerrado, con matices**: los seis puntos están implementados y verificados (ver
  §10). Lo que queda es endurecer el default operativo, no volver a construir el gate.
- **Recomendación**: compartir el repo está bien; compartirlo como “listo para producción
  autónoma” exige cambiar el default a `strict`/`never` y revisar los roots. La base ya
  está.
- **Harness² no cambia el veredicto**: aporta la taxonomía c1..c8, el contrato de edición
  validado, el ledger de presupuesto y la biblioteca por dominio, pero es POSIX-only, no
  ejecuta `nexus-v3` y no cubre approval/grants/MCP/resumen durable. Se usa como
  referencia de diseño, no como runtime ni dependencia.

## 12. El grafo como foso: qué hay medido y qué falta

La ventaja diferencial de `nexus-v3` no es “guardar todo”: mem0, Hindsight, Engram y el
event log de OpenHands también persisten. La ventaja es un **grafo tipado, con
procedencia y ligado al resultado**, que se recupera en la tarea siguiente. Medido hoy:

| Métrica | Valor | Fuente |
|---|---|---|
| Nodos | 8.890 | `brain/knowledge_graph.db` (read-only) |
| Aristas | 27.169 | idem |
| Entidades | 12.869; 42.180 vínculos | idem |
| Aristas `shared_entity` | 26.772 (98,5%) | co-ocurrencia automática |
| Aristas semánticas | 360 `relates_to`, 36 `supersedes`, 1 `contrasts` | idem |
| Nodos con `revision_count>0` | 6.645 | idem |
| Filas en `node_revisions` | **0** | no hay versión anterior restaurable |
| Títulos repetidos | 2.180 grupos / 4.822 filas (54%) | idem |
| Llamadas a `recall` | 20 de 590 tool calls (3,4%) | `data/event_store.db`, 11–20 oct |
| Llamadas a `learn` | 15 de 590 (2,5%) | idem |
| Nodos creados en 2 días de import | 7.872 (88,5%) | 2026-08-30 y 2026-10-11 |

Tres conclusiones que se siguen de esos números:

1. **El grafo escribe mucho y lee poco.** El ciclo de aprendizaje cierra al escribir
   (`record_turn` → judge → `LearningGraph`/`KnowledgeMemory`, `learning_service.py`),
   pero la lectura automática existía solo de nombre: `_run_agent_once` sí llamaba a
   `_knowledge_block`, pero con **un único FTS5 con AND sobre la frase completa**. Medido
   sobre 4 consultas reales en español: **0 hits en 4/4**. `prompt_assembler.build_system_prompt()`
   acepta `memory_snapshot`, pero ningún llamador lo usa; `rag.search()` solo se usa desde
   endpoints y el bridge viejo. (Corregido el 2026-10-20: ver §12.1.)
2. **La capa semántica es fina.** 98,5% de las aristas son co-ocurrencia de entidades.
   Solo 36 aristas `supersedes` en 8.890 nodos: el grafo no expresa todavía “esta lección
   reemplaza a aquella decisión” ni “este fix deriva de este fallo”.
3. **“Infinita” hoy significa acumulativa, no evolutiva.** Sin filas en `node_revisions`,
   el nodo guarda la versión vigente y se pierde la evolución. El volumen además es
   mayormente importado, no aprendido: tras los dos imports, el crecimiento orgánico es
   de decenas de nodos por semana (48 el 13-oct, 11 el 19-oct).

### Qué convierte el almacenamiento en ventaja

1. **Inyección por defecto, no por decisión del modelo** (P1): en `build_system_prompt`,
   extraer entidades/términos del turno, `fts_search` + `find_related` sobre 2–3 nodos y
   `latest_topic` del proyecto activo; renderizar un bloque `MEMORIA RELEVANTE` de
   ≤700 tokens con `id` de nodo para que sea auditable. Si no hay hits, no se inyecta.
2. **Telemetría de recuperación**: `fts_search` no toca `access_count` y `touch()` no
   tiene llamadores; hoy no se puede medir si el grafo sirve. Envolver la búsqueda y
   emitir `memory_used` con los ids citados. Métricas: `hit_rate`, `reuse_lift`
   (éxito/costo con y sin hit), `precision` por muestra.
3. **Aristas tipadas al escribir**: `learn` con `topic_key` ya crea `supersedes`; falta
   `derived_from` (nodo → sesión/tarea) e `informed_by` (decisión → hallazgo). Sin eso el
   grafo no explica por qué el agente sabe lo que sabe.
4. **Consolidación real**: `maintain()` deduplica por `content_hash` exacto; no mergea
  near-duplicates ni archiva versiones. Sueño/consolidación debe puntuar por uso
   (`access_count`) y promover o degradar nodos.
5. **Medir el foso**: A/B de la suite E2E y de sesiones reales con inyección on/off. Sin
   esa curva, “memoria infinita” es una afirmación de almacenamiento, no de capacidad.

Harness² entra acá como **cliente del grafo**, no como store paralelo: sus entradas
`cue/lesson/evidence/confidence` se escriben con `category='harness'` y
`topic_key='harness/c1..c8'`, y sus veredictos como aristas `improves`/`reverts` contra el
nodo de la propuesta. Así el ledger de harness y la memoria comparten procedencia.

### 12.1 Implementación del ciclo de lectura (2026-10-20)

Se implementó la inyección automática, no declarativa:

- `src/core/memory_context.py`: extrae términos (sin código, URLs, stopwords ES/EN con
  folding de acentos), busca, filtra por coincidencias exactas, recorta por presupuesto de
  caracteres y emite telemetría. Si no hay hits o `memory.inject.enabled=false`, devuelve
  `""` y el turno sigue igual.
- `KnowledgeMemory.search_terms()`: consulta cada término por separado, pondera por IDF
  (`log(1 + N/(1+df))`) y fusiona con RRF. El FTS anterior unía todo con AND; por eso las
  frases largas daban cero. `matches` se verifica sobre el contenido completo del
  candidato, no sobre el top-8 de cada término (con ese tope la intersección daba 0).
- Dedupe en recuperación por contenido normalizado, porque `content_hash` no es confiable:
  los nodos 181 y 4042 tienen texto idéntico (992 chars) y hashes distintos.
- `KnowledgeMemory.touch_many()`: incrementa `access_count`/`last_accessed` de los nodos
  inyectados. `status()` ahora expone `uses`, `used_nodes`, `last_used`.
- Evento `memory_used` con `node_ids`, `terms`, `query_len`, `injected_chars` y
  `session_id`; permite medir hit rate y auditar qué memoria entró al prompt.
- Config `memory.inject` (enabled, limit 3, max_terms 10, min_terms 2, min_matches 2,
  max_chars 2400, snippet_chars 320) + schema.

Medición antes/después contra el grafo real (8.890 nodos), con las mismas 4 consultas:

| Consulta | Antes (FTS AND) | Después |
|---|---|---|
| “¿cómo funciona el approval gate de nexus-v3…?” | 0 | 2 nodos, 522 chars |
| “arreglá el timeout del test de tsc en ui” | 0 | 3 nodos, 729 chars |
| “qué decidimos sobre el grafo de conocimiento y la memoria” | 0 | 3 nodos, 645 chars |
| “implementar inyección automática de memoria con telemetría” | 0 | 2 nodos, 539 chars |

El bloque real inyectado (end-to-end vía `src.api.server._knowledge_block`):

```text
MEMORIA DEL GRAFO (recuperada automáticamente; usá estos datos exactos y citá el [id]...):
- [11223] (concept · 2026-10-19) Memoria y Conocimiento - El «grafo» de conocimiento...
- [181] (general · 2026-08-30) # REGLA DE MEMORIA (directiva del usuario)...
- [7264] (knowledge · 2026-10-11) REGLA DE MEMORIA...
```

Y el evento quedó registrado en `data/event_store.db`:

```json
{"kind":"memory_used","session_id":"probe-e2e-memory",
 "data":{"node_ids":[11223,181,7264],"terms":["grafo","conocimiento","memoria"],
         "query_len":57,"injected_chars":645}}
```

Ese evento es de la primera pasada; con el código final `data` incluye además
`rerank` (`off|lexical|semantic|fallback`) y `rerank_ms`. En un turno real
(`smoke-gate-deny`) el evento registró `"rerank":"semantic","rerank_ms":90` con
`node_ids=[1745,6029,11104]`.

Verificación (2026-10-20, backend :8765 reiniciado con el código nuevo):

- `pytest -q`: **648 collected, 645 passed, 0 fallos, 0 errores, 3 skipped**, exit 0;
  log en `data/pytest-final.log`.
- `scripts/e2e_diagnostic.py`: **total=41 pass=41 fail=0**, exit 0.
- `check_config()`: `valid=true`, `errors=[]` (schema `memory.inject` incluido).
- `GET /api/graph/status`: `uses=5`, `used_nodes=4`, `last_used=2026-10-20T21:36:01Z`.
- 9 tests nuevos en `tests/test_memory_context.py`, incluida la regresión de dedupe con
  `content_hash` histórico distinto y contenido idéntico.
- 5 tests nuevos en `tests/test_durable_memory.py` (resumen durable, aristas tipadas,
  rerank y fallback).

### 12.1.1 Aristas tipadas y rerank (segunda pasada)

- `KnowledgeMemory.learn(..., session_id, informed_by)` crea `derived_from` hacia un
  nodo estable `session:<id>` (`ensure_session_node`) y `informed_by` hacia los nodos
  que se inyectaron en el prompt. Son best-effort: un fallo de arista no pierde el nodo.
- `learn_tool` pasa `session_id` y `registry.current_memory_node_ids` (poblado por
  `_knowledge_block` con `collect`); `_distill_run` pasa `session_id`; `/api/learn`
  acepta `session_id` e `informed_by`. Así el grafo registra procedencia, no solo datos.
- Rerank `auto|off|force`: usa embeddings locales (`nomic-embed-text`), combina coseno
  con el score RRF y respeta `rerank_timeout_ms`/`rerank_candidates`. En `auto`, si no
  hay embeddings o hay timeout, cae al orden léxico sin romper el turno. La telemetría
  `memory_used` ahora incluye `rerank` y `rerank_ms`.
- Evidencia: `tests/test_durable_memory.py` (resumen tras “reinicio”, aristas
  `derived_from`/`informed_by`, rerank reordena, fallback y `off`). Probe con memoria
  falsa: el orden pasó de `[1,2]` a `[2,1]` y `touch_many([2,1])`.

Límites que quedan: (a) la recuperación base sigue siendo léxica; el rerank semántico es
opcional y cae a léxico si no hay embeddings, así que no garantiza recall semántico;
(b) `min_matches=2` prioriza precisión sobre recall; (c) el grafo tiene ruido de imports
(nodos de prueba, 54% de títulos repetidos), así que la calidad de la memoria depende de
consolidar el dato, no solo el código.

### 12.2 `semantic-procedural-memory-ai-agent`: veredicto

Repo revisado: `AmOrFeU86/semantic-procedural-memory-ai-agent` (commit `377ac4c`,
2026-01-05). Son ~220 líneas en 4 archivos: memoria en un JSON con tope de 30 hechos,
dedupe por substring/Jaccard, extracción cada N mensajes con un LLM vía OpenRouter. No usa
embeddings, no hay grafo, no hay tests y **no tiene licencia**. “Procedural” es una línea
del prompt (“procedimientos aprendidos”), no un store ni un planificador.

No aporta código reutilizable. La única idea que coincide con lo implementado es la
deduplicación por similitud al escribir; acá se resolvió mejor en la lectura (hash del
contenido normalizado). Veredicto: **no adoptar**.

### 12.3 P1/P2 implementados (tercera pasada, 2026-10-20)

Se cerraron los P1/P2 de §10 con fallbacks explícitos: nada nuevo es dependencia dura.
En esta tercera pasada los extras de tree-sitter y `pywinpty` todavía no estaban instalados
en `.venv-ft314`; el repo-map usaba regex + PageRank y el PTY era opt-in con fallback.
La cuarta pasada (§12.4) instala y cablea ambos backends reales.

| Ítem | Implementación | Evidencia |
|---|---|---|
| P1.7 compactación | `CompactionPipeline.summary_budget()` = `max(min_tokens, limit*ratio)` con techo; reutiliza el resumen durable si `progress_messages < minimum_progress_messages`; cooldown tras fallo; preámbulo de handoff; hook `on_compact`. `compact(include_summary_fallback=False)` permite que el L4 resuma el "middle" original en vez de perderlo en el snip. | `tests/test_p1_p2_hardening.py` (presupuesto, reuso sin LLM, handoff, cooldown). |
| P1.8 guardrail de secuencia | `src/core/sequence_guard.py`: HIGH/MEDIUM/LOW heurístico; en `mode: auto` solo consulta el LLM si la secuencia es sospechosa (MEDIUM); HIGH determinístico no gasta llamada. Integrado en `ToolRegistry.dispatch` después del gate HITL. HIGH = deny; MEDIUM = aprobación o fail-closed. Sin `ApprovalContext` se omite por compatibilidad; los caminos no interactivos ya pasan un contexto `interactive=False`. La política formal declarativa llega en §12.4. | `tests/test_p1_p2_hardening.py` (HIGH `rm -rf /`, repetición MEDIUM, LOW no llama LLM, LLM solo en sospecha). |
| P2.9 poda de resultados | `CompactionPipeline.prune_for_llm()`: cabeza+cola de tool results viejos, conserva los últimos N, no muta el historial persistido. `AgentLoop` poda solo el payload que manda al LLM. | Test de poda: original intacto, últimos resultados intactos. |
| P2.10 PTY | `src/tools/pty_shell.py`: `NEXUS_PTY=1` + `pywinpty`; si falta o falla, cae a `PersistentShell`. La tool `pty_exec` solo se registra si el PTY está activo y disponible; por defecto no aparece (27 tools en `/api/tools`). La sesión persistente llega en §12.4. | Test de fallback `echo pty_fallback_ok`; `/api/tools` sin `pty_exec` por defecto. |
| P2.11 repo-map | `src/core/repo_map.py`: símbolos + imports + PageRank, presupuesto de tokens, fallback regex. Tool `repo_map` (read-only) registrada y visible en `/api/tools`. El backend tree-sitter real llega en §12.4. | Probe real: `src/core` con `budget_tokens=300`, `edges=12`, `tree_sitter=false`; test de presupuesto y ranking. |
| P2.12 anti-loop configurable | `agent_loop.anti_loop` (`repeat_warn`, `repeat_stop`, `failures_warn`, `failures_stop`). Producción usa `[3,5]/8`; los tests que instancian `AgentLoop` sin config conservan los defaults históricos `[2,3]/4` para no cambiar el contrato del doom-loop. | Test de atributos configurados; suite existente de doom-loop sigue pasando. |
| P2.13 sandbox ladder | `scope.py`: alias `read-only`, `workspace-write`, `danger-full-access`; `Scope.escalate(target, approved=True)` solo sube, nunca baja; `permission_level` y `permission` normalizado. | Test de aliases, escalado sin aprobación (deniega), con aprobación (sube) y downgrade (deniega). |
| P2.14 `brain_recall` con presupuesto | `brain_recall(..., budget=low|medium|high, max_tokens=)` y tool `recall` recortan el payload y devuelven `budget.used_tokens`/`truncated`; las escrituras ya usan `asyncio.to_thread`. | Código compilado; suite y e2e en verde. |

Verificación (2026-10-20, backend :8765 reiniciado con el código nuevo):

- `pytest`: **656 passed, 3 skipped, 0 fallos**, exit 0 (174 s); log en `data/pytest-final.log`.
  11 tests nuevos en `tests/test_p1_p2_hardening.py`.
- `scripts/e2e_diagnostic.py`: **total=41 pass=41 fail=0**, exit 0; log en `data/e2e-p1p2.log`.
- `check_config()`: `valid=true`, `errors=[]` (schema con `agent_loop.anti_loop`, `guard.sequence`,
  `compaction.summary/prune` y alias de `scope.permission`).
- `GET /api/status`: 200, `running=[]`, `approvals_pending=0`.
- `GET /api/approvals/grants`: 200, `{"grants":[]}`.
- `POST /api/chat` (`smoke-p1p2`): `success=true`, `reply="ok"`, `iterations=1`.
- `GET /api/tools`: 27 tools; `repo_map` presente; `pty_exec` ausente por ser opt-in.

Límites que quedan (tercera pasada): (a) el guardrail de secuencia heurístico no reemplaza
una política de seguridad formal; su valor es que el actor no decide su propio riesgo y que
las secuencias sospechosas pasan por aprobación. (b) El repo-map sin tree-sitter es
aproximado (regex) y el PageRank es sobre imports, no sobre tipos. (c) El PTY no conserva
estado entre comandos (cada `pty_exec` abre un proceso nuevo); para estado está
`exec`/`PersistentShell`.

### 12.4 Cuarta pasada: los 4 pendientes cerrados (2026-10-20)

Se cerraron los cuatro límites técnicos de §12.3. Nada de esto es dependencia dura: si el
extra no está, el comportamiento anterior (regex / `PersistentShell` / heurística) sigue.

| Pendiente | Implementación | Evidencia |
|---|---|---|
| tree-sitter real | `repo_map` usa `tree_sitter` + gramáticas individuales (`tree_sitter_python`, `tree_sitter_javascript`, `tree_sitter_typescript`) con cache de parser por sufijo, nombres por campo `name`, offsets en bytes (UTF-8 correcto) y fallback regex. Extras `repomap` en `pyproject.toml`. | `stats.tree_sitter=true`; probe `src/core`: 108 archivos, 20 aristas, 270 tokens con presupuesto 300; tests de símbolos Python/JS/TSX y Unicode. |
| `pywinpty` real | `pywinpty==3.1.0` instalado; `run_pty()` ahora usa un PTY real y `run_command(prefer_pty=True)` usa la sesión persistente. Si `pywinpty` falta, fallback a `PersistentShell`. Extra `pty` en `pyproject.toml`. | `pty_available()==true`; `echo hola_pty` devuelve `hola_pty`; `cmd /c exit 7` devuelve 7; timeout devuelve 124/`interrupted=true`. |
| PTY persistente | `PtySession`: un `cmd.exe` interactivo en ConPTY por proceso; conserva cwd, variables de entorno y procesos. Marcador por sesión (`__NEXUS_DONE_<token>__%errorlevel%`), limpieza de OSC/CSI y del eco de consola, timeout que cierra la sesión. `pty_exec` solo cambia de cwd si el caller lo pide; sin `cwd` conserva el actual. | Probe API: `set NEXUS_API_TEST=xyz789` y luego `echo %NEXUS_API_TEST%` devuelve `xyz789`; `cd /d` persiste entre llamadas; `/api/tools` con `NEXUS_PTY=1` muestra 28 tools (incluye `pty_exec`), sin la variable muestra 27. |
| Política formal de secuencia | `SequenceGuard` acepta `guard.sequence.policy`: reglas declarativas inline o en archivo YAML (`deny/high/medium/approve/allow/low`), `tools` con globs, `pattern` sobre comando o args, `path_pattern`, `case_sensitive`. Entre las reglas que matchean gana la más severa (deny > high > medium > allow); el `allow` explícito salta la heurística. Los secretos también se detectan en argumentos (`read_file .env` → MEDIUM). | Tests de política (deny gana sobre allow, allow read-only, archivo externo, secreto en args); `config.yaml` trae 7 reglas (4 deny, 2 medium, 1 allow). |

Verificación de la cuarta pasada:

- `pytest`: **664 passed, 3 skipped, 0 fallos**, exit 0 (113,43 s); log `data/pytest-4pend.log`.
  19 tests en `tests/test_p1_p2_hardening.py` (8 nuevos de esta pasada).
- `scripts/e2e_diagnostic.py`: **total=41 pass=41 fail=0**, exit 0; log `data/e2e-4pend.log`.
- `check_config()`: `valid=true`, `errors=[]` (schema incluye `guard.sequence.policy`).
- Backend :8765 reiniciado (PID nuevo): `/api/status` 200, `/api/approvals/grants` `{"grants":[]}`,
  `POST /api/chat` `success=true`/`reply="ok"`, `/api/tools` 27 tools con `repo_map` y sin
  `pty_exec` (opt-in).
- Probe PTY vía `_build_registry(...).dispatch("pty_exec", ...)`: `echo pty_api_ok` → `pty_api_ok`;
  `set NEXUS_API_TEST=xyz789` + `echo %NEXUS_API_TEST%` → `xyz789`; `cmd /c exit 5` → `exit_code=5`.

Nota de instalación en Python 3.14 free-threaded (Windows): no hay wheels `cp314t` para las
gramáticas. Se compilaron desde sdist con parches mínimos: `tree-sitter-python 0.25.0` aportando
`src/scanner.c` (el sdist lo omite), `tree-sitter-typescript 0.23.2` aportando
`common/scanner.h` y `tree_sitter/parser.h` de tree-sitter v0.23.2, y quitando el tag `abi3`
forzado. En un Python normal, `pip install -e '.[repomap,pty]'` usa wheels y no requiere parches.

Trade-off del GIL: en Python free-threaded, importar `tree_sitter._binding` habilita el GIL del
proceso porque la extensión no declara `Py_MOD_GIL_NOT_USED` (verificado:
`sys._is_gil_enabled()` pasa de `False` a `True`). El import es lazy: solo ocurre al usar
`repo_map`. Para conservar el free-threading, `NEXUS_REPOMAP=regex` fuerza el fallback regex y
evita importar tree-sitter.

