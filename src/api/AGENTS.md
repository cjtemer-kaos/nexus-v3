# AGENTS.md — src/api

`server.py` = FastAPI monolítico (todos los endpoints). Reglas:

- **No importa `json` a nivel módulo** (usa alias `_json2` en algunos sitios): en código nuevo
  hacé `import json as _json` LOCAL dentro de la función.
- **Body**: usá `await _json_body(request)` (devuelve 400 si el JSON es inválido) en vez de
  `request.json()`.
- **Construcción estándar**: `_load_config_safe()` (NO `load_config`, expande `${ENV}`),
  `_build_router_from_config(cfg)`, `_build_scope(cfg, agent_id, permission_override=)`,
  `_build_registry(scope, session_id, cfg, router=...)`, `_run_agent_once(...)`.
- **Tools**: `registry.register_function(fn, name=, description=, category=, parameters=)`
  con JSON Schema. Para delegación: `agent_run`/`gema_run`; sub-agentes: `sub_agent`.
- **Stream**: `_sse_stream_response` registra el run en `_RUNNING` (steer) y limpia en finally.
  `time` NO está importado → `__import__("time")` o import local.
- **Plan mode / steer / status**: `_SESSION_MODES`, `_RUNNING`, `_steer_drain`, `GET /api/status`.
- Al agregar un endpoint, REINICIÁ el backend (8765) o la UI no lo verá.
- Verificación: `scripts/e2e_diagnostic.py` (esperado `fail=0`).
