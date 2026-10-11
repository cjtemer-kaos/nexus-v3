# PI_INTEGRATION — Qué aprendimos de `pi` y cómo está aplicado en nexus-v3

Fuente: [earendil-works/pi](https://github.com/earendil-works/pi) — harness minimal y extensible
(Mario Zechner / badlogicgames), optimizado para LLM locales. Aprendizajes aplicados aquí,
integrados con las herramientas que nexus-v3 ya tenía.

## Aplicado y verificado

| Técnica de pi | Dónde | Cómo se activa | Evidencia |
|---|---|---|---|
| **Prompt-cache stickiness** | `src/core/agent_loop.py` (`run`) | fija el modelo 1 vez por run via `router.resolve(hint)`; `NEXUS_STICKY_MODEL` | `test_sticky_model_single_per_run` |
| **Prefijo estable** | `agent_loop` (system prompt estático + tools primero) | — | cache-friendly por diseño |
| **Ollama `keep_alive` + `num_ctx`** | `src/providers/ollama_provider.py` | `providers.ollama.{keep_alive,options}` | `ollama ps` → CONTEXT 8192, UNTIL 29m; recarga 9.79s vs caliente 6.32s |
| **llama.cpp `cache_prompt` + `max_tokens`** | `src/providers/llamacpp_provider.py` | `providers.llamacpp.{max_tokens,options}` | `cache_prompt:true` en el payload |
| **Streaming token-a-token** | `provider_base.stream` + `{ollama,llamacpp}_provider.stream` + `router.stream` + `agent_loop._stream_turn` + SSE `/api/chat/stream` + UI `chatStreamApi` | siempre | SSE: `{delta:h}{delta:ola}{reply:hola}`; E2E navegador |
| **Parallel read-only tools** | `agent_loop` (`_READONLY_TOOLS`) | `NEXUS_PARALLEL_TOOLS` / `concurrency.parallel_tools` | 6 tools: 1.81s → 0.30s (~6x) |
| **Free-threading (sin GIL)** | `.venv-ft314` (Python 3.14t) | `scripts/run_ft.bat` | 8 hilos CPU: 0.78s → 0.18s (~4.3x) |
| **Tree sessions (branch/fork)** | `src/core/session_log.py` (`fork`/`branch`/`tree`) + endpoints | `POST /api/sessions/{id}/fork|branch`, `GET /api/sessions/{id}/tree` | fork→hijo; tree ancestros/hijos |
| **Constrained sampling (cloud)** | `src/providers/cloud_provider.py` (`_strict_tools`) | `providers.cloud.strict_tools` (opt-in) | tools strict (OpenAI) |
| **Compaction reversible** | ya existía (`session.db` append-only; compact = vista) | — | originales preservados |
| **Skills on-demand** | ya existía | — | progressive disclosure |

## Estado de eficiencia visible
`GET /api/runtime` → `{concurrency:{free_threaded,cpu,workers}, tuning:{ollama,llamacpp,sticky_model,parallel_tools}}`.

## No aplicable / diferido
- **Constrained sampling en local** (Ollama/llama.cpp no soportan constraints de tool-calls): solo cloud strict.
- **TUI differential rendering**: nexus-v3 usa UI web, no TUI.
- **Classifier por logprobs** (`llama-cpp-classify`): posible mejora futura de routing.

## Cómo correr en modo free-threaded (recomendado)
```
uv python install cpython-3.14.7+freethreaded
uv venv --python cpython-3.14.7+freethreaded .venv-ft314
uv pip install --python .venv-ft314/Scripts/python.exe -r requirements-ft.txt
scripts\run_ft.bat
```
Fallback: Python 3.13 estándar (sin free-threading).
