# AGENTS.md — src/core

Núcleo del harness. Cambios acá afectan a TODO. Reglas:

- **agent_loop.py**: loop TDAO. Si tocás el orden de mensajes/prompt, no rompas:
  (1) system prompt, (2) ENTORNO, (3) AGENTS.md jerárquico, (4) plan-mode. Los tres últimos
  se anexan a `messages[0]`.
- **provider_router.py** + **adaptive_router.py**: UNIFICADOS. El aprendizaje vive en el
  singleton `get_adaptive_router()` (persiste en `data/router_state.json`). NO crees otro
  AdaptiveRouter: usá `get_adaptive_router()`. `_record_success/_record_fail(provider, model, latency)`.
- **scope.py**: TODA tool FS pasa por acá. `permission=full` libera; `read_only` (plan mode)
  bloquea mutantes; `require_confirm_mutating` exige `confirm=true`.
- **tool_registry.py**: `dispatch` es async y await-ea handlers. `_infer_schema` usa
  `get_type_hints` (no anotes tipos como strings). Registrá tools como `async def` si son I/O.
- **diagnostics.py** + **lsp_client.py**: diagnóstico post-edit (py_compile + pylsp). Degrada si no hay server.
- **checkpoints.py**: snapshot ANTES de write/edit. **diffutil.py**: diffs. **board.py**: tareas/claims.
- **session_log.py**: WAL+FTS; `delete_session` tiene guard FK (no orfanar hijos → 409).
- **session_janitor.py**: dueño del borrado y la auditoría completos. `delete_session()` de acá
  (no el de `session_log`) es el que usa la API: se lleva `todos`, `goals`, grants y los
  directorios derivados. Si agregás un artefacto por sesión (nueva DB, nuevo dir), sumalo a
  `collect()`/`_orphan_plan()`/`delete_session()` y exponé su ruta desde el módulo dueño.
- **event_store.py**: fuente de eventos; `append`/`get_events`. No lo bloquees.

Regla de oro: corré `py_compile` + `pytest -q` tras tocar cualquier archivo de acá.
