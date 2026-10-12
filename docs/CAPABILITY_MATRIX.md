# Matriz de capacidades — Nexus v3 vs asistentes/IDE de IA

Fecha: 2026-10-12. Fuentes: documentación de Cursor, Claude Code, Copilot, Windsurf, Codeg, MonkeysCode,
agent-code, AgentBridge (JetBrains) + inventario propio del código de nexus-v3.

Leyenda: ✅ implementado · ⚠️ parcial (vía otra tool/exec) · ❌ no existe.

## 1. Edición de código / agente

| Tarea (competencia) | Estado | Evidencia en nexus-v3 |
|---|---|---|
| Chat / Q&A sobre el repo | ✅ | `/api/chat`, `agent_loop`, UI `ChatPanel` |
| Editar multi-archivo (agent mode) | ✅ | `agent_loop` + tools `write_file`/`edit` |
| Edición quirúrgica (str_replace/replace_in_file) | ✅ | tool `edit` (reemplazo único, `replace_all` opcional) |
| Autocompletado inline (Tab) | ❌ | Nexus es harness+UI propia, no plugin de editor |
| Indexado semántico del código (RAG) | ✅ | `rag_engine`, `codebase_context`, `code_search`, `/api/rag/*` |
| Búsqueda de símbolos/referencias | ⚠️ | `code_search`, `codegraph_tool` (sin LSP nativo) |
| Gestión de contexto / compactación | ✅ | `compaction` (4 capas), `trajectory_compressor`, `session_distill` |
| Plan mode | ✅ | `plan_mode`, `/api/plan` |
| Iterar sobre errores (self-correct) | ✅ | `agent_loop` (replan, doom-loop guard) |
| Test runner + parseo de fallos | ⚠️ | Vía tool `exec` (sin runner/parse dedicado) |
| Git (status/diff/commit/branch/PR) | ⚠️ | Vía `exec` (sin tools git dedicadas) |
| Diff review (UI) | ❌ | No hay vista de diff |
| Checkpoints / rollback de FS | ⚠️ | `session_log.fork/branch`, `event_store` (sin snapshot de archivos) |

## 2. Multi-agente / orquestación

| Tarea | Estado | Evidencia |
|---|---|---|
| Subagentes / delegación | ✅ | `sub_agent_spawner`, tools `gema_run`/`agent_run`, `agent_runner` |
| Agentes en paralelo | ✅ | `concurrency`, `dag_coordinator`, `mixture_of_agents`, `/api/parallel` |
| Hooks (automatización determinista) | ✅ | `hooks_engine` (10 fases, 3 gates) |
| Skills reutilizables | ✅ | `skill_gateway`/`skill_install`/`skill_opt` + catálogo |
| MCP (tools externas) | ✅ | `mcp_client`/`mcp_connection_manager`, tools `mcp_list`/`mcp_call` |
| Instrucciones de proyecto (AGENTS.md) | ⚠️ | Referencias parciales; sin carga explícita estilo CLAUDE.md |
| Sesiones: save/resume/fork/branch/compact | ✅ | `session_log` (`tree`/`fork`/`branch`), `/api/sessions/*` |
| Rewind de conversación | ❌ | No implementado |

## 3. Modelos / infraestructura

| Tarea | Estado | Evidencia |
|---|---|---|
| Multi-modelo local + cloud + BYOK | ✅ | `provider_router`, `provider_catalog`, providers ollama/cloud/llamacpp |
| Routing por capacidad (tools / visión) | ✅ | `provider_router` (`supports_tools`/`supports_vision`) |
| Multimodal (imagen / screenshots) | ✅ | `vision_gem_v2`, `/api/upload`, `supports_vision` |
| Permisos / approval gates | ✅ | `scope`, `approval_gate`, `/api/permission` |
| Sandbox de ejecución | ✅ | `sandbox_service` |
| Streaming (SSE + WS) | ✅ | `/api/chat/stream`, `/ws/chat` |
| Búsqueda web | ✅ | Tool `web_search` (open source: **SearXNG** vía `SEARXNG_URL`, fallback **Wikipedia**); `web_fetch` para URL directa |
| Automatización de navegador | ✅ | `cdp_browser`, MCP playwright/chrome-devtools |
| Voz (STT/TTS) | ✅ | `voice_engine`, `/api/voice/*` |
| Generación de imagen (ComfyUI) | ✅ | `comfyui_gateway`, `gallery` |
| Editor de archivos (Monaco) + explorar PC | ✅ | `FileEditor` (barra de ruta, unidades, árbol) |
| Terminal integrada | ✅ | `TerminalPanel` + tool `exec` |

## 4. Diferenciadores de Nexus (lo que no tiene la competencia)

| Diferenciador | Estado | Evidencia |
|---|---|---|
| **Aprendizaje en tiempo real** | ✅ | `learning_service` (judge + Thompson), `self_learning_loop`, `skill_opt` (held-out), `dream_distill`, `/api/learning/*` |
| **Memoria grafo (larga duración)** | ✅ | `knowledge_memory` (grafo ~8.6k nodos), `hybrid_memory`, `hierarchical_memory` (3 tiers + forgetting), `/api/graph/*`, `rag_engine` |
| **Local + cloud combinados** | ✅ | `provider_router` (tier/Thompson/VRAM), fallback cross-provider, BYOK catalog |
| **Colmena (usar cualquier programa como ayudante)** | ✅ | `nexus_hive`, `message_board`, `agent_registry`, peers (claude-code, antigravity, openclaw, zero-code, aider, hermes), `sub_agent_spawner` |

## 5. Gaps priorizados (a cerrar)

1. ~~**Tool `edit` (str_replace)**~~ — **CERRADO**: agregada tool `edit` (reemplazo quirúrgico).
2. ~~**Búsqueda web**~~ — **CERRADO**: tool `web_search` (SearXNG open source vía `SEARXNG_URL` + fallback Wikipedia).
3. **Tools git** (status/diff/commit/branch) dedicadas — hoy solo vía `exec`.
4. **Test runner** con parseo de fallos — hoy solo `exec`.
5. **Diff review en la UI** — no hay vista de diff.
6. **Instrucciones de proyecto** (carga de AGENTS.md por sesión).
7. **Rewind** de conversación.

> Bug sistémico corregido: `_infer_schema` mapeaba toda anotación string (`from __future__ import annotations`)
> a `"string"` → los params `int` (exec.timeout, recall.limit…) se declaraban string. Ahora se resuelven con
> `get_type_hints`.

## 6. Conclusión

Nexus cubre **el núcleo agéntico** (chat, multi-archivo, tools, RAG, plan, subagentes, MCP, sesiones,
multi-modelo local/cloud, multimodal, voz, navegador, editor, terminal) y **gana en los 4 diferenciadores**
(aprendizaje en vivo, memoria grafo, híbrido local+cloud, colmena). Los gaps son de **conveniencia**
(edit quirúrgico, web search, git/test dedicados, diff UI), no de arquitectura.
