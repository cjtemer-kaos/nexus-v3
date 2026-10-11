# Auditoría del harness Nexus v3 — 2026-10-11

Auditoría de 12 capas (ECC `agent-architecture-audit`) + benchmark contra harnesses de
mercado (opencode, Hermes, Claude Code, Codex, OpenHands, Goose, aider) y la literatura
2026 (OpenAI *Harness Engineering*, Anthropic *Effective Context Engineering* /
*Effective Harnesses for Long-Running Agents*, Berkeley CS294 Lecture 25, 12-Factor Agents,
UNU *Engineering and Governing the Agent Harness*).

## Veredicto inicial

`high_risk` — **harness ornamental, no load-bearing**: el harness del `BLUEPRINT.md` estaba
bien diseñado y testeado, pero la ruta viva `POST /api/chat` lo corría **sin compactación,
sin hooks y sin event-store**; y `src/core/` tenía **194/270 módulos huérfanos (~62k líneas
de SuperNEXUS v2)**.

## Hallazgos (severidad)

| # | Sev | Hallazgo | Evidencia |
|---|-----|----------|-----------|
| 1 | critical | `AgentLoop` de producción construido sin `compaction`/`event_store`/`hooks` | `server.py:2190` (antes) vs `agent_loop.py:398-407,474-482,709-719` |
| 2 | critical | 3-gate de seguridad (AIDefence + prompt_security + scope) no corría en chat; `ai_defence`/`prompt_security` huérfanos | `BLUEPRINT.md:189`, `hooks=None` |
| 3 | high | 194 módulos huérfanos (~62k líneas v2) | análisis de alcanzabilidad AST |
| 4 | high | tool discipline prompt-only; registry inline (9 tools, sin MCP ni skills) | `agent_loop.py:330-338`, `server.py:1928-2077` |
| 5 | high | memory poisoning: `learn` con `source="agent"` sin gate + auto-persist por turno | `server.py:2027`, `learning_service.py:253` |
| 6 | high | sin manifiesto de dependencias | raíz |
| 7 | medium | tools parseadas de prosa (`_extract_text_tool_calls`) ejecutables | `agent_loop.py:137-257` |
| 8 | medium | `summary` = reply crudo truncado | `agent_loop.py:748` |
| 9 | medium | event-store no cableado → sin replay/auditoría | — |
| 10 | medium | sin evals/benchmarks | — |

## Correcciones aplicadas

1. **Wiring en producción** (`server.py`): `_event_store()`, `_build_compaction(cfg)`,
   `_build_hooks(cfg)`; pasados a `AgentLoop` en `/api/chat` y `_run_agent_once`.
   - `_build_compaction` resuelve `persist_dir` a raíz del proyecto (offload a `./data/spill`).
   - `_build_hooks` registra el gate `pre_tool_use` con `prompt_security.is_suspicious_content`.
2. **Gate load-bearing** (`tool_registry.dispatch`): honra `STOP`/`COMPACT` del hook en
   **código** (no prompt) → `PermissionError`. Antes el resultado del hook se ignoraba.
3. **Validación de argumentos** (`tool_registry.validate_args`): requeridos, tipos y
   `additionalProperties` antes de ejecutar.
4. **Log estructurado por call**: `registry.call_log` → `EventStore` (kind `tool_call`).
5. **MCP real** (`src/core/mcp_client.py`): cliente stdio (SDK `mcp`, servidores del
   bundle, just-in-time, loop de fondo) + tools `mcp_list`/`mcp_call`.
6. **Admisión de memoria** (`config.memory` + gate en `learn`): `agent_write_enabled`,
   `learn_min_chars`; el resumen de sesión ya no guarda errores/echos.
7. **Purga de código muerto**: 178 módulos `src/core/*.py` → `_attic/src/core/` (registro en
   `_attic/MOVED.txt`), reversible. Limpieza de `.bak`, `.pyc` (3 versiones de CPython),
   logs y `_tmp_models.json`.
8. **Manifiesto**: `requirements.txt` + `pyproject.toml` (core + extras dev/voice/vision/rag/train).
9. **Eval/regression harness**: `tests/test_harness_eval.py` (compactación+offload,
   validación, gate load-bearing, event sourcing, memoria multi-turno, reactive compact).

## Verificación

- `python -m pytest tests/` → **exit 0** (288 tests) + 6 evals nuevos.
- `python -m src.cli.main --check-config` → `VERIFICACIÓN APROBADA`.
- E2E: `POST /api/chat` → `reply:"hola-nexus"`, `mode:"agent_loop"`.
- `data/event_store.db` poblado: `user_message`, `assistant_message`, `turn/start|end`,
  `step/start|end`, `task_completed`.
- UI (`Vite :5174` + backend `:8765`): consola sin errores.

## Pendiente (P2, no bloqueante)

- Subagentes aislados (contexto propio + depth cap) como tool `delegate`.
- Migraciones de DB versionadas.
- Evals con snapshots de sesión grabada (hoy: eval de contrato).
- Verificación en vivo del MCP cliente contra un servidor real (`npx`).

## Referencias de diseño

- OpenAI, *Harness engineering: leveraging Codex in an agent-first world* (2026-02).
- Anthropic, *Effective context engineering for AI agents*; *Effective harnesses for long-running agents*.
- Berkeley CS294, *Lecture 25: Software Harness Design* (2026).
- HumanLayer, *12-Factor Agents*.
- UNU, *Engineering and Governing the Agent Harness* (2026-07).
