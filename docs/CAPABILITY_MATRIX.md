# Matriz de capacidades — Nexus v3 vs asistentes/IDE de IA

Fecha: 2026-10-19. Fuentes: documentación de Cursor, Claude Code, Copilot, Windsurf, Codeg, MonkeysCode,
agent-code, AgentBridge (JetBrains) + inventario verificado del código de nexus-v3 (suite + E2E).

Leyenda: ✅ implementado · ⚠️ parcial (vía otra tool/exec) · ❌ no existe.

## 1. Edición de código / agente

| Tarea (competencia) | Estado | Evidencia en nexus-v3 |
|---|---|---|
| Chat / Q&A sobre el repo | ✅ | `/api/chat`, `agent_loop`, UI `ChatPanel` |
| Editar multi-archivo (agent mode) | ✅ | `agent_loop` + tools `write_file`/`edit` |
| Edición quirúrgica (str_replace/replace_in_file) | ✅ | tool `edit` (reemplazo único, `replace_all` opcional) |
| Autocompletado inline (Tab) | ❌ | Nexus es harness+UI propia, no plugin de editor |
| Indexado semántico del código (RAG) | ✅ | `rag_engine`, `codebase_context`, `code_search`, `/api/rag/*` |
| Búsqueda de símbolos/referencias | ⚠️ | `code_search` (regex) + `lsp_client` (pylsp) si está instalado; `codegraph_tool` NO sirve acá (su backend `src/codegraph/` no existe en v3) |
| Gestión de contexto / compactación | ✅ | `compaction` (4 capas), `trajectory_compressor`, `session_distill` |
| Plan mode | ✅ | `plan_mode`, `/api/plan` |
| Iterar sobre errores (self-correct) | ✅ | `agent_loop` (replan, doom-loop guard) |
| Test runner + parseo de fallos | ⚠️ | Vía tool `exec` (sin runner/parse dedicado) |
| Git (status/diff/commit/branch/PR) | ⚠️ | Vía `exec` (sin tools git dedicadas) |
| Diff review (UI) | ✅ | `ReviewPanel` (Cambios) + `/api/checkpoints/{id}/diff` |
| Checkpoints / rollback de FS | ✅ | `checkpoints.snapshot` antes de cada `write_file`/`edit` + `/api/checkpoints/restore` |
| Lista de tareas persistente por sesión | ✅ | tools `todo_write`/`todo_read` (`todo_store`, tabla `todos` en `session.db`), `GET/POST /api/todos` |
| Comandos propios en markdown | ✅ | `custom_commands.py`: cada `.md` en `.nexus/commands/` (o `~/.nexus/commands/`) es `/nombre`, con `$ARGUMENTS`, `$1..$9` y `@ruta` acotado al proyecto; `GET/POST /api/slash` refresca en cada llamada |
| Formateo tras escribir | ✅ | `formatters.py` + hook en `agent_loop` después de `write_file`/`edit`/`apply_patch`: `ruff` del venv o PATH, `prettier` local (nunca `npx`), `gofmt`, `rustfmt`; por argv, con timeout, nunca lanza. Bloque `formatters:` en `config.yaml` (enabled/timeout_s/commands) con schema. Reporta `formatted` en el resultado del tool; `clean`/`skipped` no agregan ruido |
| Generar AGENTS.md (`/init`) | ✅ | `project_init.py` + `/init [ruta] [force]`: stack, comandos de test/build/run y estructura leídos del disco (`pyproject`/`package.json`/`Cargo.toml`/`go.mod`, con BOM tolerado); no pisa un AGENTS.md existente sin `force` y respeta el peldaño de permisos (se niega con #0/#1) |

## 2. Multi-agente / orquestación

| Tarea | Estado | Evidencia |
|---|---|---|
| Subagentes / delegación | ✅ | `sub_agent_spawner`, tools `gema_run`/`agent_run`, `agent_runner` |
| Agentes en paralelo | ✅ | `concurrency`, `dag_coordinator`, `mixture_of_agents`, `/api/parallel` |
| Hooks (automatización determinista) | ✅ | `hooks_engine` (10 fases, 3 gates) |
| Skills reutilizables | ✅ | `skill_gateway`/`skill_install`/`skill_opt` + catálogo |
| MCP (tools externas) | ✅ | `mcp_client`/`mcp_connection_manager`, tools `mcp_list`/`mcp_call` |
| Instrucciones de proyecto (AGENTS.md) | ✅ | `agent_loop` inyecta jerárquico (raíz → subcarpeta) en `messages[0]`; `test_agents_md_hierarchical` |
| Sesiones: save/resume/fork/branch/compact | ✅ | `session_log` (`tree`/`fork`/`branch`), `/api/sessions/*` |
| Rewind de conversación | ✅ | `POST /api/sessions/{id}/rewind` + `GET .../tree` |
| Board de tareas con claims | ✅ | `board.py` claim-once (`UPDATE` + `rowcount`) + lease con vencimiento/heartbeat; `/api/board/*` |

## 3. Modelos / infraestructura

| Tarea | Estado | Evidencia |
|---|---|---|
| Multi-modelo local + cloud + BYOK | ✅ | `provider_router`, `provider_catalog`, providers ollama/cloud/llamacpp |
| Routing por capacidad (tools / visión) | ✅ | `provider_router` (`supports_tools`/`supports_vision`) |
| Multimodal (imagen / screenshots) | ✅ | `/api/upload` → `AgentLoop(images=)` → modelo con `supports_vision`; tool `image_describe` (lee una imagen de disco con el modelo de visión local y devuelve texto, para cuando el canal es solo texto) |
| Permisos / approval gates | ✅ | `scope`, `approval_gate`, `/api/permission` |
| Sandbox de ejecución | ✅ | `sandbox_service` |
| Streaming (SSE + WS) | ✅ | `/api/chat/stream`, `/ws/chat` |
| Búsqueda web | ✅ | Tool `web_search` (open source: **SearXNG** vía `SEARXNG_URL`, fallback **Wikipedia**); `web_fetch` para URL directa |
| Automatización de navegador | ✅ | `cdp_browser`, MCP playwright/chrome-devtools |
| Voz (STT/TTS) | ⚠️ | TTS: Piper (`voice_models/piper`) o fallback **Windows SAPI** (sin descargas), con botón **Escuchar** en el chat. STT: endpoint HTTP `:8789` o `faster-whisper` local. `/api/voice/*` |
| Generación de imagen (ComfyUI) | ✅ | `comfyui_gateway`, `gallery` |
| Editor de archivos (Monaco) + explorar PC | ✅ | `FileEditor` (barra de ruta, unidades, árbol) |
| Terminal integrada | ✅ | `TerminalPanel` + tool `exec` |

## 4. Diferenciadores de Nexus (lo que no tiene la competencia)

| Diferenciador | Estado | Evidencia |
|---|---|---|
| **Aprendizaje en tiempo real** | ✅ | `learning_service` (judge + Thompson), `self_learning_loop`, `skill_opt` (held-out), `dream_distill`, `/api/learning/*` |
| **Memoria grafo (larga duración)** | ✅ | `knowledge_memory` (grafo ~8.6k nodos), `hybrid_memory`, `hierarchical_memory` (3 tiers + forgetting), `/api/graph/*`, `rag_engine` |
| **Historial y restore del grafo** | ✅ | `node_revisions` (append-only); cada upsert archiva la versión saliente; `GET /api/graph/node/{id}/history` + `POST .../restore` (restore re-indexa FTS5 y entidades) |
| **Local + cloud combinados** | ✅ | `provider_router` (tier/Thompson/VRAM), fallback cross-provider, BYOK catalog |
| **Colmena (usar cualquier programa como ayudante)** | ✅ | `nexus_hive`, `message_board`, `agent_registry`, peers (claude-code, antigravity, openclaw, zero-code, aider, hermes), `sub_agent_spawner` |

## 5. Gaps priorizados (a cerrar)

1. ~~**Tool `edit` (str_replace)**~~ — **CERRADO**: agregada tool `edit` (reemplazo quirúrgico).
2. ~~**Búsqueda web**~~ — **CERRADO**: tool `web_search` (SearXNG open source vía `SEARXNG_URL` + fallback Wikipedia).
3. ~~**Tools git** (status/diff/commit/branch) dedicadas~~ — **CERRADO**: `src/tools/git_tools.py`
   + tools `git_status`, `git_diff`, `git_log`, `git_commit` (argv, nunca shell; commit solo local,
   sin push/pull/reset). Ver `docs/PRIME_AGENT_AUTOPSY.md` §1.2.
4. ~~**Test runner** con parseo de fallos~~ — **CERRADO**: `src/tools/test_runner.py`
   (pytest/npm/cargo/go, conteos + fallos parseados + timeout honesto) expuesto como `run_tests`.
   Ver `docs/PRIME_AGENT_AUTOPSY.md` §1.3.
5. ~~**Diff review en la UI**~~ — **CERRADO**: `ReviewPanel` (Cambios) con `/api/checkpoints/{id}/diff`.
6. ~~**Instrucciones de proyecto** (AGENTS.md)~~ — **CERRADO**: inyección jerárquica en `agent_loop`.
7. ~~**Rewind** de conversación~~ — **CERRADO**: `POST /api/sessions/{id}/rewind`.

> Bug sistémico corregido: `_infer_schema` mapeaba toda anotación string (`from __future__ import annotations`)
> a `"string"` → los params `int` (exec.timeout, recall.limit…) se declaraban string. Ahora se resuelven con
> `get_type_hints`.

**Estado 2026-10-27**: §5 queda sin gaps abiertos. Lo único pendiente son opcionales por hardware
(STT local) y el roadmap de `docs/PRIME_AGENT_AUTOPSY.md` §3 (continual harness, cron/heartbeats,
mailbox tipado, skills ejecutables), que son features nuevas, no paridad.

## 6. Conclusión

Nexus cubre **el núcleo agéntico** (chat, multi-archivo, tools, RAG, plan, subagentes, MCP, sesiones,
multi-modelo local/cloud, multimodal, voz, navegador, editor, terminal, checkpoints con diff y rewind)
y **gana en los 4 diferenciadores** (aprendizaje en vivo, memoria grafo con historial/restore,
híbrido local+cloud, colmena). Los gaps que quedan son de **conveniencia** (tools git y test runner
dedicados) y **opcionales por hardware** (STT local), no de arquitectura.

## 7. Verificación (2026-10-19)

| Prueba | Resultado |
|---|---|
| Suite completa (`pytest`) | **1.592 tests en 137 archivos, exit 0** (2026-10-27; `--collect-only -q` + corrida completa sin fallos) |
| Skips | 3 honestos: no existe `cerebro.db` de v2 (el lector se cubre con brain sintético) |
| Diagnóstico E2E (`e2e_diagnostic.py`) | **total=41 pass=41 fail=0** |
| Modo app (Electron) | backend en puerto dinámico, `/api/health` 200, `ui/dist` servido, bundle servido == disco, favicon 200 |
| TTS | Piper o **Windows SAPI**; verificado: WAV 22050 Hz mono de 4.83 s vía `POST /api/voice/tts` |
| Board | claim-once con `UPDATE`+`rowcount`; carrera de 6 agentes → gana exactamente 1; lease con heartbeat |
| Grafo | `node_revisions` append-only; history/restore verificado (v2 → restore → v3, con FTS5 al día) |
| Portabilidad del brain | precedencia `NEXUS_V3_BRAIN` > `config.brain.path` > `./brain`; `NEXUS_BRAIN` (v2) se ignora; `/api/health` → `linked_to_v2:false` |
| Validación de entrada | board (title/priority/status/lease) y `/api/chat/stream` (message) → **400** verificado en vivo; antes caían a 200 |
| Worktree | solo se acepta la **raíz** de un repo; una subcarpeta → `ok:false` con la raíz correcta (evita crear worktrees en un repo ancestro) |
| Config | `config.yaml` valida contra el schema; declaradas las claves que el server ya leía (`event_db_path`, `require_confirm_mutating`, `auto_apply`, `budget`) |
| UI | busy/errores en Board, Approvals, Worktrees y restore del grafo; buscar en el editor; TTS “Escuchar”; sin respuestas canned al fallar el backend |
| Comandos slash | el menú del chat se alimenta de `GET /api/slash` (18 comandos del server, incluye `/herramientas`); lista local solo como fallback |
| Estados honestos | footer “sin conexión” (rojo) si `/api/status` falla; error visible en sesiones, uso, capacidades, keys y permisos en vez de “sin datos” |
| Checkpoints | `POST /api/checkpoints/restore` → 400 sin `checkpoint_id`, 404 si no existe (antes 200 con `ok:false`) |
| OpenAI compat | `/v1/chat/completions` → 400 sin `messages`/sin mensaje de usuario y 500 si el turno falla (antes 200 con “Error: …”); stream documentado como buffered |
| Tests nuevos (2ª pasada) | **65** en 7 archivos: `schema_sanitizer`, `prompt_security`, `think_scrubber`, `todo_store`, `worktree`, `brain_link`, `config_schema` |
| Tests nuevos (3ª pasada) | **13** más: `goal_store` (8 unit) + 5 E2E (`/api/goals`, `/goal` persistente, códigos de error de fork/rewind/tree, `/api/plugins/reload`) → total **78** en 8 archivos |
| Tests nuevos (4ª pasada) | **43** más: `mcp_client` (7: scrub de credenciales para subprocesos + manifiesto), `error_envelope` (10: clasificación/decisión/backoff/retry), `task_queue` (8: DAG, ciclos, niveles), `session_scrubber` (6: alternancia y no-mutación), `bounded_history` (5: tope/TTL/coherencia), `decision_engine` (3) + 4 E2E (búsqueda global con `session_id`, approvals 404, worktree no-repo 400, embed 400) |
| `/goal` | **persiste de verdad** (antes decía “goal registrado” sin guardar nada): `GoalStore` en `session.db`, `GET/POST /api/goals`, y `/goal status` devuelve el objetivo real |
| Ramas de sesión | `GET tree` → 404 si no existe; `POST fork` → 404 si la sesión no existe; `POST rewind` → 400 sin `message_id` / no entero; errores → 500 (antes todo 200 con `ok:false`) |
| Plugins | `POST /api/plugins/reload` informa `available/loaded/errors/failed` y devuelve `ok:false` si un plugin rompe (antes `ok:true` siempre); la UI muestra el fallo |
| Voz | `POST /api/voice/stt` y `/tts` → **503** (no 501) cuando el servicio no está disponible; `VoiceMic` cae al STT del navegador con 503/501 |
| Streaming | SSE y WS informan `success:false` + `error` (antes el WS devolvía el texto “Error: …” como si fuera respuesta del modelo); el chat prefija ⚠️ cuando el modelo no respondió o hubo `echo_fallback` |
| Slash en el chat | `/export` descarga el ZIP de la sesión, `/todo` lista las tareas reales, `/agent` cambia de ayudante y `/session` crea sesión (antes solo se mostraba texto) |
| Errores honestos (3ª auditoría) | `GET /api/events` y `GET /api/sessions` devolvían **200 con lista vacía + error** (indistinguible de “no hay nada”): ahora 500 con `detail`. `POST /api/approvals/{id}` → 404/409/500 según el caso (antes 200 `ok:false`). `POST /api/worktree` → 400 (no es repo) / 409 (git falló). `POST /api/embed` → 400 con input inválido (antes 502, culpando al servicio) |
| Búsqueda global | `/api/sessions/global/search` devuelve `session_id` (nuevo `SessionLog.search_hits`): al hacer clic en un resultado la UI **abre esa sesión** (antes solo cerraba el buscador) |
| Sesiones en la UI | “+ Nueva sesión” ya no dice “bifurcar” mintiendo: hay botón aparte **Bifurcar la sesión activa** (`POST /api/sessions/{id}/fork` con `copy_messages`). Borrar una sesión con hijas (409) ya no la quita del listado en falso: se muestra el motivo |
| Estados de error en la UI | Chat muestra banner con **Reintentar** si falla el historial (antes chat vacío); Sub-agentes distingue “sin conexión” de “no hay sub-agentes”; el detalle del grafo informa el fallo; la recarga de plugins muestra qué plugin rompió |
| Política de errores | `TimeoutError` ahora es reintentable (faltaba `timeouterror` en la lista y `"timed out"` en los patrones: un timeout real se abortaba sin reintentar) |
| Integridad de datos | `SequenceScrubber` ya **no muta** los dicts del llamador; `BoundedHistory.__len__` cuenta solo lo vigente (antes contaba expirados y contradecía a `get_all()`) |
| Módulos legacy | `src/core/decision_engine.py` importaba `src/core/command_protocol` (inexistente): el módulo era **in-importable**. Ahora degrada a un `Command` local; verificado con `decompose()` |
| `web_fetch` (revisión + cableado) | `HTTPFetcher` no validaba la **URL inicial** (SSRF directo a `127.0.0.1`/metadata), su `redirect_request` devolvía un string (contrato roto) y **escapaba** el HTML (`<`→`&lt;`) en vez de convertirlo. Ahora: SSRF en cada salto (incluye IPv4-mapped, multicast y unspecified), redirects manuales validados, HTML→texto legible, y **la tool `web_fetch` del registry usa esta clase** en vez del loop duplicado inline (con `max_length=15000` y el fence de contenido no confiable) |
| Tests nuevos (5ª pasada) | **39** en 5 archivos: `credential_pool` (4), `llm_provider` (9: costos, cadena de failover, barrera de tool-state), `mcp_pool` (10: refcount, evicción, health, reconexión, singleton), `dag_coordinator` (10: patrones, dependencias, niveles) y `lsp_client` (5: framing JSON-RPC real por stdio contra un servidor falso + `format_diagnostics`) |
| Fallos reales de la 5ª pasada | `CredentialPool.mark_error` tomaba el lock y llamaba a `record_failure` (el mismo `threading.Lock`, no reentrante) → **deadlock del failover** con una API key configurada. `envelope.classify()` no existe (es `ErrorClassifier.classify`) → la excepción dejaba la tarea `PENDING` y el DAG informaba **`completed`** con todo fallado; además un nodo fallido **no bloqueaba a sus hijos** (ahora `SKIPPED`). El pool MCP no detenía el servidor evacuado (proceso huérfano) y hacía `await` con el lock tomado en `reconnect`; los `reconnects` se perdían al recrear la conexión. `format_diagnostics` lanzaba `IndexError` si un diagnóstico no traía `message` |
| Cobertura sin red | RAG se prueba sin Ollama con un embedder falso (`test_rag_indexa_y_busca_sin_ollama`); el probe de Ollama ya no corre al importar el módulo. Los E2E resuelven la raíz con `Path(__file__).parents[1]` en vez de `os.getcwd()`. `asyncio.iscoroutinefunction` (deprecado, removible en 3.16) → `inspect.iscoroutinefunction` en hooks y sub-agentes |
| Config que ahora SÍ manda (6ª pasada) | `agent_loop.error_policy` nunca se aplicaba (`set_error_policy` no se llamaba); `heartbeat_ms`/`max_iterations` se ignoraban en 2 de 4 call sites; la compactación usaba `32000`/`0.75` fijos en vez de `session.max_tokens`/`compact_threshold`; `scope.max_read_bytes` no tenía efecto porque `effective_max_bytes()` devolvía el default de `max_file_read_bytes`. Todo cableado con `_new_agent_loop()` y `ScopeConfig.effective_max_bytes()` |
| Portabilidad de datos | `checkpoints` y `provider_models` guardaban en `./data/...` **relativo al CWD**: si el backend arrancaba desde otra carpeta, los snapshots/caché quedaban fuera del proyecto. Ahora cuelgan de la raíz del proyecto (`data_home()`) salvo env explícito |
| Embeddings / RAG | `embeddings.embed_url()` hacía `re.search` sobre el YAML crudo: ignoraba `OLLAMA_HOST`, los `${ENV}` y el parser de config. Ahora usa `environment.ollama_base_url()` (OLLAMA_HOST > `providers.ollama.embed_url` > `base_url` > default) |
| `POST /api/sessions/{id}/branch` | acepta `copy_messages` por **query y por body JSON** (antes el body se ignoraba en silencio: rama vacía sin error) |
| `WS /ws/chat` | contrato **JSON simétrico** en éxito y error: `{success, reply, session_id}` / `{success:false, error, reply:""}` (antes éxito = texto crudo) |
| `/v1/chat/completions` (stream) | el chunk de error incluye `error: {message, type}` además del texto, así un cliente OpenAI puede detectarlo; `stream` sigue siendo **buffered** (documentado en el docstring) |
| Steer en la UI | con un run en curso, Enter ya no queda bloqueado: envía la instrucción a `POST /api/sessions/{id}/steer` y muestra si quedó encolada. Antes el endpoint era inalcanzable desde la UI |
| Endpoints programáticos (sin UI) | cubiertos por E2E (`test_get_endpoints_programaticos_sin_ui`): `/api/cloud/free-models`, `/api/rag/index-status`, `/api/rag/stats`, `/api/learning/stats`, `/api/learning/recommend`, `/api/skill-opt/stats`, `/api/skill-opt/heldout`, `/api/runtime`, `/api/graph/status`. Los mutantes (`rag/index-graph`, `graph/maintain`, `learning/record`, `skill-opt/{propose,validate}`, `dream/run`) son **API programática**, no hay pantalla que los invoque |
| Dependencias | `pyproject.toml` declara `ruamel.yaml` (lo usa `save_config` para preservar comentarios; sin él, cada guardado de config los perdía) y el extra `lsp` (`python-lsp-server`). Sin pylsp el diagnóstico post-edit sigue con `py_compile`; el semántico degrada, no rompe |
| Re-auditoría de tests v2 | los 10 "sin cobertura" ya estaban cubiertos salvo `self_learning_loop` (**5 tests nuevos**: learn/cycle, judge, router, persistencia SQLite, patrones, stats). `test_config` ya usa `monkeypatch`+`tmp_path` (el claim de `/tmp` hardcodeado era del informe v1); `test_e2e_live` ya no usa `os.getcwd()`. Los `sleep` fijos de `test_subagent_sprint` (5 s), `test_audit_fixes` y `test_phase9_learning` se cambiaron por polling acotado (sin carreras de 40-120 ms) |
| Re-auditoría UI v2 | 10 de 12 hallazgos ya estaban corregidos en el árbol (errores de historial/borrado/búsqueda/grafo visibles, `parent_id` real vía `forkSessionApi`, `SLASH_FALLBACK` en español con `/herramientas`, `sessionMessagesApi` limit 10000, `tabs.tsx` oscuro, dobles submits con `busy`). Se cerró lo que faltaba: `/api/status` expone `approvals_pending` y la campana lo muestra con badge (antes ignoraba aprobaciones); enums de sub-agentes en español; se quitó el tipo muerto `images` de `chatApi` y el `sessionModeApi` duplicado |
| `repl`: kernel de Python persistente (idea central de prime-agent) | una sola tool con **estado entre llamadas**: `6*7` en una celda → `42` en la siguiente, mismo pid; timeout mata el kernel y avisa que el estado se perdió (la próxima celda arranca limpia); reciclado por `idle_s`; `repl.enabled=false` la desactiva. Tests: 10 en `test_python_repl.py` |
| Tools git por argv + `run_tests` | cierran §5.3 y §5.4: `git_status/diff/log/commit` parsean `--porcelain=v1 -z` (rutas con espacios y renombres), commit solo local y con mensaje como único `-m`; `run_tests` detecta pytest/npm/cargo/go y devuelve conteos + fallos parseados. Verificado por MCP real (`scripts/verify_new_tools_mcp.py`): 52 tools, `git_status` con head real, `run_tests` → `passed: 13` |
| Parseo real de pytest | el resumen de pytest **no siempre** viene con `====` (con `-q` es `1 failed, 1 passed in 0.08s`): el regex viejo devolvía `counts: {}` en una corrida real aunque los tests sintéticos pasaran. Ahora acepta con/sin decoración, toma la última coincidencia e infiere por progreso (`.FsEx` con o sin `[ NN%]`) |
| Checkpoints: id repetido | el id usaba `time.time_ns() % 1_000_000`; el reloj puede repetir valor en llamadas seguidas → dos snapshots del mismo archivo compartían nombre y **el segundo pisaba al primero** (undo intermedio perdido). Ahora busca un nombre libre y hay test con reloj congelado |
| Comandos en markdown | **15 tests** en `test_custom_commands.py` (frontmatter, `$ARGUMENTS`/`$1..$9`, `@ruta` dentro y fuera del proyecto, include recortado, CRLF, nombre inválido, builtin protegido). En el `:9000` vivo: `GET /api/slash` → 18 comandos con `/revisar` (`origin: custom`); `POST /api/slash` con `/revisar src/core` → `action: send_prompt` y el prompt con `Alcance: src/core` expandido |
| Comando roto no rompe el listado | un `.md` con nombre inválido, duplicado o que choca con un builtin se saltea con el motivo (`errors`/`skipped`) y el resto sigue funcionando; `/api/slash` refresca el registro en cada llamada, así un comando nuevo aparece sin reiniciar el backend |
| Contrato de transporte | **la fuente es Python**: `src/core/api_contract.py` parsea `server.py` con `ast` (no regex) y `scripts/gen_api_contract.py` escribe `ui/src/lib/api-contract.generated.ts` → **139 rutas** con método, path/query params, claves de body que el handler lee, claves del return, `bodyOpen` (patch parcial) y `stream`, más un hash de versión (`70132782ee27`). `GET /api/contract` compara el fuente contra `app.routes` en caliente: `only_in_code=[]`, `only_in_runtime=[]` verificado en la app viva (`:11726`). La UI reexporta el contrato desde `lib/api.ts` y en `import.meta.env.DEV` avisa si `apiFetch` pide una ruta inexistente; el artefacto no se tipea a mano (lo dice el encabezado) |
| Frescura del contrato | `tests/test_api_contract.py` (**13** tests): `render()` == archivo en disco, `--check` real por subprocess (exit 1 si derivó, exit 0 al día), rutas `/api/*` de los 46 archivos de la UI declaradas en el contrato, sin duplicados método+path, y `/api/contract` runtime == generado. Un rename de ruta en `server.py` rompe el test, no la app |

### Código legacy absorbido (no es el camino del harness)

`src/bridges/*`, `src/tools/codegraph_tool.py`, `src/core/gallery.py`,
`src/core/nexus_hive.py`, `src/core/llm_provider.py` y `src/core/dag_coordinator.py` vienen de
proyectos anteriores (v2/autopsia). Los dos últimos ya tienen suite propia y sus bugs corregidos
(deadlock de credenciales, DAG que no bloqueaba dependencias), pero el loop TDAO no los usa:
el camino del harness es `provider_router` + `agent_loop`. Sus dependencias opcionales
(`networkx`, `Pillow`, `redis`, `paramiko`, `mss`, `cv2`, `pytesseract`, `gfpgan`…) están
**guardadas con try/except** o son lazy, así que su ausencia no rompe nada y el harness no las
necesita para funcionar. Se dejan como referencia (regla: no borrar); si se cablean, declarar
los extras en `pyproject.toml`.

**codegraph (2026-10-25)**: `codegraph_tool.py` no se puede usar en v3 — `build_graph` importa
`src.codegraph.builder`, paquete que **no existe** acá, y `_get_graph` necesita `networkx`, que no
está instalado. Por eso se quitaron del bridge MCP las 6 tools `codegraph_*` que lo llamaban
(anunciaban una capacidad que siempre fallaba). El archivo queda como referencia exportable
(`EXPORT_GAP.md`, P0); si se quiere la feature, hay que traer `src/codegraph/` + `networkx`.

### Ronda de autopsia de harness (2026-10-19)

Comparación contra los repos de `D:\ias\autopsia` (openharness, byo-coding-agent, goose, aider,
opencode, deepseek-harness, agent-zero, OpenHands, mimo-code, hermes-agent, grok-build, engram).
Se implementó lo que el árbol realmente no tenía:

| Mejora | Evidencia / comportamiento |
| --- | --- |
| Deny duro de secretos | `scope.HARD_DENY_PATTERNS` se **une** a `deny_patterns`: `permission: full` ya no puede leer `.ssh`, `.aws`, `.env`, `*.pem`. Antes la lista de `config.yaml` **reemplazaba** los defaults y dejaba esos patrones afuera |
| Compactación que no rompe pares | `_snip` retrocede el corte si el primer mensaje retenido es `role=tool` (arrastra su `assistant(tool_calls)`); `_micro` conserva los últimos `micro.keep_recent` resultados (default 3) en vez de colapsar el contexto vivo del turno |
| Resumen L4 conectado | `agent_loop` usa `summarize_with_llm(...)` cuando hay router (antes era **código muerto**: solo se llamaba `compact()`). Prompt con 9 secciones fijas (pedido, decisiones, archivos, comandos, errores, estado, pendientes, próximo paso, datos) y parseo de `<summary>` |
| Spill de salidas grandes | `src/core/spill.py`: resultado > 8000 chars → cabeza 62% + cola 38% + archivo completo en `data/spill/<sesión>/` con la ruta para releer. Antes se cortaba **el final**, donde están los errores |
| Errores accionables | `_error_hint()` (permiso/timeout/archivo/args) + sugerencias con `difflib` para tools inexistentes. Los reintentos se resetean al éxito y 3 fallos consecutivos inyectan un recordatorio; 6 abortan el run |
| `read_file` paginado | `offset`/`limit` por líneas con header (`líneas 1-80 de 240; seguí con offset=80`) |
| `list_dir` recursivo | `recursive=true` + tope configurable |
| Tool `glob` | `glob(pattern, path)` con jail por scope y cap de 500 |
| Tool `apply_patch` | Parche multi-archivo **atómico** (`*** Begin Patch` / `*** Update File:` / hunks `@@`). Valida todos los hunks antes de escribir, `dry_run=true`, checkpoint por archivo y rollback completo si falla |
| `edit` tolerante | Normaliza CRLF/LF para comparar y **restaura el estilo** del archivo; el error pide releer el archivo |
| Jobs en background | `job_start`/`job_read`/`job_wait`/`job_kill`/`job_list` (`src/core/jobs.py`): builds/tests/dev-servers ya no bloquean el turno; lectura por cursor (delta), mismo `_blocked_command` + jail que `exec`, kill del árbol (`taskkill /T /F`) |
| Sandbox endurecido | `SandboxService` ya **no hereda todo `os.environ`** (allowlist; `inherit_env=True` es opt-in), mata el árbol en timeout y recorta salida cabeza+cola (`max_output_bytes`) |
| RAG con diversidad | MMR (Jaccard de tokens, λ=0.7) sobre el top-k: evita 5 chunks casi idénticos del mismo archivo |
| Modo plan persistente | `_SESSION_MODES` sobrevive al reinicio en `data/session_modes.json` (solo se guardan las sesiones en `plan`) |
| Selector de modo en la UI | botón **Act/Plan** en el chat con `sessionModeApi` + refresco de `/api/status`; antes el endpoint existía pero era inalcanzable |
| `scripts/hygiene.py` | Backup **consistente** de las bases del brain con `sqlite3.backup()` (no `copy` con WAL activo), retención por familia (`--keep`, default 3), exclusión de `rag_index.db` (regenerable), limpieza de `workspace/` (`__pycache__`, dirs vacíos, `-p`/`pkg`) y reporte de `vendor/llama.cpp`. Dry-run por defecto; `--apply` para ejecutar |
| `vendor/` ordenado | `llama.cpp` (556 MB, sin uso desde la migración a Ollama) se movió a `D:\ias\backups\nexus-v3\vendor-llama.cpp-20261019`; `vendor/README.md` documenta procedencia, restauración y verificación |
| Tests | **15 nuevos** en `tests/test_harness_upgrades.py` (deny duro, snip/micro, spill, jobs, apply_patch atómico, MMR, modo plan persistente) → suite **623 passed, 3 skipped**; E2E **41/41** |

Implementado en la misma ronda (portabilidad: sin supuestos de hardware ni de provider):

- **Extracción de memoria automática por turno** (`memory.auto_extract`): al cerrar un turno
  exitoso, `MemoryExtractor` analiza los últimos `window` mensajes (default 8) y guarda hasta
  `max_facts` hechos (default 5) con dedupe. Usa **el provider que resuelva el router** (local o
  cloud, el que haya); sin provider cae al extractor regex y no rompe nada. Corre en background
  (`asyncio.create_task`), así no suma latencia al chat. Se apaga con
  `memory.auto_extract.enabled: false`.
- **Subconjunto de tools por sub-agente** (`SubAgentSpec.allowed_tools`): `sub_agent(tools="a,b")`
  acota el registro del hijo; si no se especifica, se aplica un set curado por gema
  (`scholar` = lectura+web+memoria (`recall`/`learn`), `code`/`engineer` = FS+shell,
  `debugger` = lectura+shell).
  El executor del spawner transporta el dato de forma **compatible**: solo pasa `allowed_tools`
  si la firma lo declara. Menos tools = menos superficie de error y menos tokens de esquema.

