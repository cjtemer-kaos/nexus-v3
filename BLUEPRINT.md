# Nexus v3 Harness — Blueprint Profesional

> **Version:** 0.1.0-draft | **Date:** 2026-09-03 | **Status:** Blueprint (no implementation)
> **Cerebro:** local `./brain` (`knowledge_graph.db` + `rag_index.db`), opcionalmente externo via `NEXUS_BRAIN` si el usuario lo configura
> **Harnesses autopsiados:** DeepSeek Harness Cordis (50+ packages), Hermes Agent (`agent/conversation_loop.py` 3900 LOC), OpenCode (TUI+SQLite sessions), Aider/Continue/OpenHands/Engram/mem0/Lethe

---

## 1. Principios de Diseno

| Principio | Origen | Aplicacion en Nexus v3 |
|-----------|--------|------------------------|
| *Model is engine, harness is everything else* | BYO Coding Agent (libro 600 LOC) | Harness nunca contiene logica de razonamiento; solo orquesta. El modelo decide, el harness ejecuta. |
| *Agency comes from the model, not code* | Learn Claude Code | Minimizar procedural branching; confiar en tool-calling del modelo. |
| *Put agent inside container, not outside* | Confucius | Gemas/tools con acceso directo a FS via Scope; sin capas proxy innecesarias. |
| *ADD-only memory* | mem0 (abr 2026) | `observations` sin UPDATE/DELETE; topic_key upsert + soft-delete + hash dedup. |
| *12-layer harness audit* | ECC / agent-architecture-audit | Cada modulo auditable contra wrapper-regression, memory-pollution, hidden-repair-loops. |

**Golden rule:** Todo via `config.yaml` + env. Nada harcodeado. Distribuible.

---

## 2. Arquitectura Global

```
┌─────────────────────────────────────────────────────────────────────┐
│                        NEXUS v3 HARNESS                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐ │
│  │ Session  │  │  Agent   │  │   Tool   │  │  Scope   │  │Compac- │ │
│  │   Log    │◄─┤   Loop   ├─►│ Registry │◄─┤  Guard   │◄─┤  tion  │ │
│  │ (WAL+FTS)│  │(TDAO+Bud) │  │(catalog) │  │(ACL+FS)  │  │(4-layer│ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └───┬────┘ │
│       │             │             │              │             │     │
│       └─────────────┴──────┬──────┴──────────────┴─────────────┘     │
│                            │                                         │
│                   ┌────────▼────────┐                                 │
│                   │ Provider Router │  Ollama │ llama.cpp │ Cloud     │
│                   │ (Thompson+VRAM) │  :11434 │  :8080    │ :9000 Zen │
│                   └────────┬────────┘                                 │
│                            │                                         │
│         ┌──────────────────┼──────────────────┐                        │
│         ▼                  ▼                  ▼                        │
│   ┌──────────┐      ┌──────────┐       ┌──────────┐                   │
│   │  Brain   │      │   MCP    │       │    UI    │                   │
│   │cerebro.db│      │  Bridge  │       │React19+  │                   │
│   │FTS5+RAG  │      │ 38 tools │       │Vite+TWS  │                   │
│   └──────────┘      └──────────┘       └──────────┘                   │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.1 Flujo de un turno (secuencia)

```
User message
  → Scope.check(message)           // ACL, path-traversal, prompt-injection scan
  → Session.append(user msg)       // WAL write + FTS index
  → Compaction.maybe_compact()     // si tokens > threshold → L1-L4 pipeline
  → HooksEngine.on_context_build() // middleware: inject brain context
  → ProviderRouter.resolve(hint)   // tier, cost, VRAM → selecciona provider+model
  → AgentLoop.run(max_iters)       // Think-Decide-Act-Observe
       ├─► LLM call (provider)
       ├─► ToolRegistry dispatch  ──► Scope.guard(tool, args)
       ├─► Tool execution → result
       ├─► Session.append(tool result)
       └─► ErrorClassifier → RETRY/SKIP/REPLAN/ABORT
  → HooksEngine.on_task_complete()
  → Brain consolidation (ADD-only, topic_key upsert)
  → Session.persist() + EventStore.append()
```

---

## 3. Estructura de Archivos Propuesta

```
nexus-v3/
├── BLUEPRINT.md                 ← este documento
├── config/
│   ├── config.yaml              ← unica fuente de verdad (ver §4)
│   ├── config.schema.json       ← JSON Schema para validacion
│   └── .env.example
├── src/
│   ├── core/
│   │   ├── session_log.py       ← Session Log (WAL + SQLite + FTS5)     §5
│   │   ├── agent_loop.py        ← Agent Loop (TDAO + budget)            §6
│   │   ├── tool_registry.py     ← Tool Registry (catalog + dispatch)    §7
│   │   ├── scope.py             ← Scope Guard (ACL + FS jail)           §8
│   │   ├── compaction.py        ← Compaction (4-layer pipeline)         §9
│   │   ├── provider_router.py   ← Provider Router (Ollama/llama.cpp)    §10
│   │   ├── provider_base.py     ← Provider ABC + frozen dataclasses
│   │   ├── hooks_engine.py      ← Hooks middleware (Hermes/Cordis style)
│   │   ├── error_classifier.py  ← RETRY/SKIP/REPLAN/ABORT
│   │   ├── iteration_budget.py  ← budget por turno (Hermes)
│   │   ├── event_store.py       ← Event sourcing (append-only)
│   │   └── trajectory.py        ← Trajectory = Session + Tool timeline
│   ├── brain/
│   │   ├── link.py              ← Enlace a brain externo si se configura (NEXUS_BRAIN), fallback `./brain` local
│   │   ├── hierarchical_memory.py ← 3-tier (working/episodic/semantic)
│   │   ├── rag_engine.py        ← nomic-embed-text 768d
│   │   └── multi_signal.py      ← vector + BM25 + entities
│   ├── providers/
│   │   ├── ollama_provider.py   ← Ollama :11434
│   │   ├── llamacpp_provider.py ← llama.cpp :8080 / :8000
│   │   └── cloud_provider.py    ← OpenAI-compatible (Zen, Anthropic, etc.)
│   ├── mcp/
│   │   └── bridge.py            ← FastMCP bridge
│   └── cli/
│       └── main.py              ← CLI entry (Hermes-style, no Commander.js)
├── ui/                          ← React 19 + Vite + Tailwind + shadcn
│   ├── src/
│   └── package.json
└── tests/
    ├── test_session_log.py
    ├── test_agent_loop.py
    └── test_scope.py
```

> **Nota:** `src/brain/` NO duplica `brain/*.db`; abre el brain configurado via `NEXUS_BRAIN` (mismo patron que `src/bridges/mcp_bridge_server.py:17` en implementaciones de referencia). Ver §11.

---

## 4. Configuracion (`config/config.yaml`)

Nada harcodeado. Unico punto de configuracion.

```yaml
# config.yaml — Nexus v3
project: nexus-v3
brain:
  path: ${NEXUS_BRAIN:-./brain}   # local ./brain; configurable a ruta externa via env
  hierarchical_tiers:
    working:   { capacity: 100, ttl_h: 1 }
    episodic:  { capacity: 500, ttl_h: 72 }
    semantic:  { capacity: 0,    ttl_h: 8760 }  # 0 = ilimitado

session:
  db_path: ./data/session.db
  wal: true
  fts: true
  max_tokens: 32000
  compact_threshold: 0.75          # compacta al 75% del context window
  keep_recent: 4

agent_loop:
  max_iterations: 25
  max_tokens_per_turn: 8192
  error_policy: { retry: 3, replan: 2 }
  heartbeat_ms: 2000

scope:
  allowed_roots: ["./workspace", "./data"]
  deny_patterns: ["**/.env", "**/*.key", "**/brain/*.db-shm"]
  max_file_read_bytes: 1048576
  allow_network: false            # default deny; tools piden permiso

compaction:
  layers: [snip, micro, budget, summary]
  snip:   { keep_recent: 4 }
  micro:  { placeholder: "[tool_result collapsed]" }
  budget: { max_tool_result_chars: 2000 }
  summary: { model_hint: "simple" }  # usa modelo cheap para resumir

providers:
  ollama:
    base_url: http://localhost:11434
    timeout_s: 300
    models: [qwen2.5-coder:7b, deepseek-r1:8b, nomic-embed-text]
  llamacpp:
    base_url: http://localhost:8080
    # alternativa atomic :8000 → proxied a :8080 (ver opencode.json "llama")
    timeout_s: 300
    models: [nexus-director-v6, qwen3.5-9b, qwen3-vl-4b]
    ctx: 40960
  cloud:
    base_url: https://opencode.ai/zen
    api_key: ${OPENCODE_API_KEY}
    auth_style: bearer
    models: [muse-spark-1.2]
    enabled: false                # opt-in

router:
  strategy: thompson              # thompson | vram | tier | manual
  vram_budget_gb: 12
  prefer_speed: false
  fallback_order: [llamacpp, ollama, cloud]

hooks:
  phases: [pre_tool_use, post_tool_use, on_error, on_compact, session_start]
  security_gate: true             # 3-gate: AIDefence + prompt_security + scope
```

Validado con `config.schema.json` (JSON Schema draft-07) al arrancar.

---

## 5. Session Log

### 5.1 Aprendizajes absorbidos

| Fuente | Patron absorbido |
|--------|-----------------|
| **DeepSeek Cordis** `packages/session` | Session como entidad con `id, project, parent_session_id, summary, compact_count`; WAL + FTS; query API |
| **SuperNEXUS v2** `src/core/session_manager.py` | `SessionMessage` + `Session` dataclass; `total_tokens`, `last_active`; `SessionManager` con SQLite + thread lock |
| **Hermes** `agent/trajectory.py` + `conversation_loop.py` | `Trajectory` como log append-only de `role/tool_call/tool_result`; reparacion de tool args; sanitizacion surrogates |
| **OpenCode** | SQLite sessions + TUI; `Session` parent para branching; `message_board.db` para NexusHive |
| **Lethe** | Hippocampus = recall asociativo; blocks always-in-context |

### 5.2 Diseno

- **Store:** SQLite WAL (`data/session.db`) + FTS5 (`session_fts`) — un archivo, sin servidor.
- **Sesiones jerarquicas:** `parent_session_id` permite branching (Hermes branching + OpenCode multi-session).
- **Event sourcing opcional:** `event_store.py` append-only para replay/audit (inspirado en `src/core/event_store.py` existente).

### 5.3 Esquema

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL DEFAULT 'default',
  parent_session_id TEXT REFERENCES sessions(id),
  created_at TEXT NOT NULL,
  last_active TEXT NOT NULL,
  summary TEXT DEFAULT '',
  compact_count INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  role TEXT NOT NULL,              -- system|user|assistant|tool
  content TEXT NOT NULL,
  tool_call_id TEXT,
  tool_name TEXT,
  tokens INTEGER DEFAULT 0,
  model TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE session_fts USING fts5(content, content='messages', content_rowid='id');
```

### 5.4 Esqueleto — `src/core/session_log.py`

```python
from __future__ import annotations
import sqlite3, threading, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

@dataclass(frozen=True, slots=True)
class Message:
    role: str           # system|user|assistant|tool
    content: str
    tool_call_id: str = ""
    tool_name: str = ""
    tokens: int = 0
    model: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

@dataclass
class Session:
    id: str
    project: str = "default"
    parent_id: Optional[str] = None
    summary: str = ""
    compact_count: int = 0
    total_tokens: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_active: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class SessionLog:
    """WAL + FTS5 session log. Thread-safe. Single SQLite file."""

    def __init__(self, db_path: str | Path, wal: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        if wal:
            self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None: ...

    def create_session(self, project: str = "default", parent_id: str | None = None) -> Session: ...

    def append(self, session_id: str, msg: Message) -> int: ...

    def get_messages(self, session_id: str, limit: int = 100, offset: int = 0) -> list[Message]: ...

    def search(self, query: str, limit: int = 20) -> list[Message]: ...  # FTS5

    def compact_mark(self, session_id: str, summary: str) -> None: ...

    def fork(self, session_id: str) -> Session: ...  # branching
```

---

## 6. Agent Loop

### 6.1 Aprendizajes absorbidos

| Fuente | Patron |
|--------|--------|
| **SuperNEXUS v2** `agent_loop.py` | TDAO (Think-Decide-Act-Observe) + `ErrorAction` enum (RETRY/SKIP/REPLAN/ABORT) + `LoopStep`/`AgentLoopResult` |
| **Hermes** `conversation_loop.py` (3900 LOC) + `iteration_budget.py` + `error_classifier.py` | Budget por turno, `FailoverReason`, classificador de errores, reparacion de tool args, message sanitization, stream diag |
| **DeepSeek Cordis** `packages/core` + `packages/hooks` | Hooks `PRE_TOOL_USE/POST_TOOL_USE/ON_ERROR`; middleware CONTINUE/STOP/COMPACT/INJECT |
| **OpenHands** | Theory-of-Mind (modela intencion humana antes de actuar) — futuro: `goal_detector.py` |

### 6.2 Diseno

- **Pattern:** TDAO estricto. Cada iteracion = 1 LLM call + 0..N tool calls.
- **Budget:** `IterationBudget(max_iterations=25, max_tokens=8192)` — corta antes de loop infinito (Hermes `iteration_budget.py`).
- **Error handling:** `error_classifier.py` → RETRY (transient), SKIP (non-critical), REPLAN (bad params), ABORT (fatal).
- **Heartbeat:** `TaskHeartbeat` cada 2s para UI / NexusHive.

### 6.3 Esqueleto — `src/core/agent_loop.py`

```python
from __future__ import annotations
import asyncio, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class ErrorAction(Enum):
    RETRY = "retry"
    SKIP = "skip"
    REPLAN = "replan"
    ABORT = "abort"

@dataclass(frozen=True, slots=True)
class ToolResult:
    success: bool
    output: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class LoopStep:
    iteration: int
    phase: str          # think|decide|act|observe
    content: str
    tool_used: str | None = None
    result: ToolResult | None = None

@dataclass
class LoopResult:
    task: str
    success: bool
    final_output: str
    steps: list[LoopStep]
    iterations: int
    total_tokens: int = 0
    duration_ms: float = 0

class AgentLoop:
    def __init__(self, provider_router, tool_registry, scope, hooks, budget=None):
        self.router = provider_router
        self.tools = tool_registry
        self.scope = scope
        self.hooks = hooks
        self.budget = budget  # IterationBudget

    async def run(self, task: str, session_id: str, hint: str | None = None) -> LoopResult:
        """TDAO loop. Resuelve provider via hint, itera hasta done o ABORT."""
        route = self.router.resolve(task_hint=hint)
        # 1. THINK: llm call
        # 2. DECIDE: parse tool_calls o final answer
        # 3. ACT: scope.guard + hooks.pre_tool_use + dispatch + hooks.post_tool_use
        # 4. OBSERVE: append to session, classify error, check budget
        ...

    def _classify_error(self, tool: str, err: str) -> ErrorAction: ...
```

---

## 7. Tool Registry

### 7.1 Aprendizajes absorbidos

| Fuente | Patron |
|--------|--------|
| **SuperNEXUS v2** `tool_registry.py` (`DirectorToolRegistry`) | Vista unificada: `ai_tool|local_tool|gema|mcp|registered`; `ToolEntry` con `call_count/success_count`; rebuild desde Director |
| **Hermes** `tool_executor.py` + `tool_guardrails.py` + `toolsets.py` | Dispatcher centralizado, guardrails por tool, `toolset_distributions` (conjuntos por contexto) |
| **DeepSeek Cordis** `packages/skill` + `packages/mcp` + `packages/tool` | `skill` = prompt + tools bundle; MCP como provider de tools; `typert` para schemas |
| **ECC** | Skills + Instincts + Memory como layers del harness |

### 7.2 Diseno

- **Catalogo unificado:** Un `ToolRegistry` que agrega `local tools + MCP tools + skills`. No duplica; referencia.
- **Schema:** Cada tool expone JSON Schema (via `typert` / Pydantic) para tool-calling.
- **Dispatch:** `registry.dispatch(name, args)` → `scope.guard` → `hooks.pre_tool_use` → exec → `hooks.post_tool_use`.
- **Metrics:** `call_count`, `success_count`, `avg_latency_ms` por tool.

### 7.3 Esqueleto — `src/core/tool_registry.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable

@dataclass(frozen=True, slots=True)
class ToolEntry:
    name: str
    description: str
    category: str          # local|mcp|skill|gema
    parameters: dict = field(default_factory=dict)  # JSON Schema
    handler: Callable | None = None
    source: str = ""       # modulo origen
    call_count: int = 0
    success_count: int = 0

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}

    def register(self, entry: ToolEntry) -> None:
        self._tools[entry.name] = entry

    def register_function(self, fn: Callable, name: str | None = None,
                          description: str = "", category: str = "local") -> None:
        """Decorator-friendly: @registry.tool(description=...)."""
        ...

    def list(self, category: str | None = None) -> list[ToolEntry]: ...
    def get(self, name: str) -> ToolEntry | None: ...
    def to_openai_tools(self) -> list[dict]: ...  # para LLM tool-calling

    async def dispatch(self, name: str, args: dict, scope, hooks) -> Any:
        """Guarded dispatch: scope check → hooks pre → exec → hooks post."""
        ...
```

---

## 8. Scope (ACL + FS Jail)

### 8.1 Aprendizajes absorbidos

| Fuente | Patron |
|--------|--------|
| **SuperNEXUS v2** `tool_guardrails.py` + `agent_teams.py: _permission_gated_executor` | `IDEMPOTENT_TOOLS` vs `MUTATING_TOOLS`; `GuardrailConfig(exact_failure_block_after=5)`; permission-gated executor |
| **Hermes** `file_safety.py` + `tool_guardrails.py` | Path normalization, deny-by-default, allowed roots, traversal prevention |
| **OpenCode** | Container pattern: agente dentro del container (Confucius) |
| **DeepSeek Cordis** `packages/guard` + `packages/fs` | Guard package + FS abstraction |

### 8.2 Diseno

- **Default deny.** Todo lo no permitido explicitamente esta bloqueado.
- **Capas:** (1) path jail (solo `allowed_roots`), (2) deny globs, (3) network gate, (4) mutating-tool confirmation.
- **Normalizacion:** `path_normalize.py` (heredado de v2) resuelve `..`, symlinks, case en Windows.

### 8.3 Esqueleto — `src/core/scope.py`

```python
from __future__ import annotations
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

@dataclass(frozen=True, slots=True)
class ScopeConfig:
    allowed_roots: list[str] = field(default_factory=lambda: ["./workspace"])
    deny_patterns: list[str] = field(default_factory=lambda: ["**/.env", "**/*.key"])
    max_read_bytes: int = 1_048_576
    allow_network: bool = False
    allow_mutating_without_confirm: bool = False

class Scope denied(Exception): ...

class Scope:
    def __init__(self, cfg: ScopeConfig, project_root: Path):
        self.cfg = cfg
        self.root = project_root.resolve()

    def check_path(self, path: str | Path) -> Path:
        """Resuelve y valida que path este dentro de allowed_roots y no matchee deny."""
        ...

    def check_tool(self, tool_name: str, args: dict) -> None:
        """Valida tool + args contra politicas (ej: terminal solo en allowed_roots)."""
        ...

    def guard(self, tool_name: str, args: dict) -> dict:
        """Wrapper: valida y sanitiza args antes de dispatch. Lanza ScopeDenied si viola."""
        ...
```

---

## 9. Compaction (4-layer pipeline)

### 9.1 Aprendizajes absorbidos

| Fuente | Patron |
|--------|--------|
| **SuperNEXUS v2** `context_compactor.py` (4-layer) | L0 SecurityLingua → L1 Snip (safe split) → L2 Micro (collapse tool results) → L3 Budget (persist large outputs) → L4 Summary (1 LLM call) + Emergency ReactiveCompact |
| **Hermes** `context_compressor.py` + `conversation_compression.py` + `trajectory_compressor.py` | Curator pattern, `manual_compression_feedback`, `estimate_messages_tokens_rough` |
| **DeepSeek Cordis** `packages/compaction` | Paquete dedicado a compaction con metrics por capa |
| **Lethe** | DMN background compaction (compaction offline) |

### 9.2 Pipeline

```
Input: messages (N tokens, limit = L)
  │
  ├─ L0 SecurityLingua?  (si habilitado: comprime jailbreaks a intencion)
  ├─ L1 Snip:    keep_recent=4, drop middle → target 0.6*L
  ├─ L2 Micro:   tool results >2k → placeholder → target 0.5*L
  ├─ L3 Budget:  outputs >2k chars → persist to ./data/spill/ + preview
  ├─ L4 Summary: si aun >L → 1 LLM call (hint="simple" → cheap model)
  └─ Emergency:  si API devuelve prompt_too_long → ReactiveCompact agresivo
Output: messages' + CompactionMetrics(layers_applied, tokens_saved, ratio)
```

### 9.3 Esqueleto — `src/core/compaction.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class LayerMetrics:
    name: str
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_saved: int = 0
    ratio: float = 1.0

@dataclass
class CompactionMetrics:
    original_tokens: int = 0
    compressed_tokens: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 1.0
    layers_applied: list[str] = field(default_factory=list)
    layers: list[LayerMetrics] = field(default_factory=list)

def estimate_tokens(text: str) -> int:
    return len(text) // 4  # rough; Hermes usa estimate_messages_tokens_rough

class CompactionPipeline:
    def __init__(self, config: dict):
        self.layers = self._build_layers(config)

    async def compact(self, messages: list[dict], limit: int) -> tuple[list[dict], CompactionMetrics]:
        """Progresivo: cada capa solo si aun excede limit."""
        ...

    async def reactive_compact(self, messages: list[dict], error: str) -> list[dict]:
        """Emergency: parse context_limit desde error + compact agresivo."""
        ...
```

---

## 10. Provider Routing (Ollama / llama.cpp / Cloud)

### 10.1 Estado actual — Providers y runtime (referencia local)

```
opencode.json providers:
  nexus  → http://127.0.0.1:9000/v1  (Director v6 Q8_0 :8080 via llama.cpp)
  llama  → http://127.0.0.1:8000/v1  (atomic proxy → :8080)
  llama-direct → http://127.0.0.1:8080/v1
  zen    → https://opencode.ai/zen   (cloud free)

Ollama :11434 (11 modelos, ver AGENTS.md)
llama.cpp :8080 (4.3GB Director siempre residente, RTX 3060 12GB budget)
```

### 10.2 Aprendizajes absorbidos

| Fuente | Patron |
|--------|--------|
| **SuperNEXUS v2** `provider_base.py` (frozen dataclasses + ABC) + `provider_unified.py` (AuthStyle, ChatMessage, ToolCall, UsageInfo) + `provider_router.py` (RouterProvider + HINT_MAPPING + RoutedProvider) + `adaptive_router.py` (Thompson Sampling) + `vram_router.py` | Frozen slots dataclasses (Pincer), `AuthStyle` enum, `HINT_MAPPING` tier routing, Thompson Sampling, VRAM budget |
| **Hermes** `credential_pool.py` + `auxiliary_client.py` + `model_metadata.py` | Pool de credenciales, clientes auxiliares, `estimate_request_tokens_rough`, `parse_context_limit_from_error` |
| **DeepSeek Cordis** `packages/llm` | LLM abstraction package |

### 10.3 Diseno Nexus v3

**Interface unificada:** Todos los providers implementan `BaseProvider` (ABC) con `chat(messages, tools, model) → LLMResponse`.

```
BaseProvider (ABC)
  ├── OllamaProvider    → http://localhost:11434  (httpx, /api/chat)
  ├── LlamaCppProvider  → http://localhost:8080/v1 (OpenAI-compatible /v1/chat/completions)
  └── CloudProvider     → https://opencode.ai/zen  (OpenAI-compatible, bearer auth)
          │
          └─► ProviderRouter
                ├─ strategy=thompson: AdaptiveRouter (Thompson Sampling, aprendido de v2)
                ├─ strategy=vram:     VramRouter (RTX 3060 12GB; Director siempre residente)
                ├─ strategy=tier:     HINT_MAPPING (simple=1 … deep=10)
                └─ fallback_order: [llamacpp, ollama, cloud]
```

**Auto-detect al arrancar:**
1. Probe `GET /health` en cada `base_url`.
2. `GET /v1/models` o `/api/tags` para listar modelos disponibles.
3. Si `llama.cpp` no responde en `:8080`, intenta `:8000` (atomic proxy).
4. Ollama fallback: si modelo pedido no existe, redirige a disponible (`_resolve_model` en `ollama.py:28`).

### 10.4 Esqueleto — `src/core/provider_base.py`

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

class MessageRole:
    system = "system"; user = "user"; assistant = "assistant"; tool = "tool"

@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: str = "user"
    content: str | None = None
    tool_calls: tuple = ()
    tool_call_id: str = ""

@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str | None = None
    tool_calls: tuple = ()
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
    model: str = ""
    reasoning_content: str | None = None
    @property
    def has_tool_calls(self) -> bool: return len(self.tool_calls) > 0

class BaseProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[LLMMessage], tools: list[dict] | None = None,
                   model: str | None = None, **kw) -> LLMResponse: ...
    @abstractmethod
    async def health(self) -> bool: ...
    @abstractmethod
    async def list_models(self) -> list[str]: ...
```

### 10.5 Esqueleto — `src/core/provider_router.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

HINT_MAPPING: dict[str, int] = {
    "none": 0, "simple": 1, "categorization": 2, "coding": 3,
    "analysis": 4, "reasoning": 5, "research": 6,
    "planning": 7, "architecture": 7, "vision": 8, "creative": 9, "deep": 10,
}

@dataclass(frozen=True, slots=True)
class ModelRoute:
    id: str; provider_name: str
    context_window: int = 32000; tier: int = 0

@dataclass(frozen=True, slots=True)
class RoutedProvider:
    provider: BaseProvider; model: str; route: ModelRoute

class ProviderRouter:
    def __init__(self, vram_budget_gb: float = 12):
        self._providers: dict[str, BaseProvider] = {}
        self._models: list[ModelRoute] = []

    def register_provider(self, name: str, p: BaseProvider) -> None: ...
    def register_model(self, route: ModelRoute) -> None: ...

    def resolve(self, task_hint: str | None = None,
                preferred_model: str | None = None,
                prefer_speed: bool = False) -> RoutedProvider | None:
        """Tier ≥ hint_complexity; Thompson/VRAM si habilitado; fallback_order."""
        ...

    async def auto_detect(self) -> dict[str, bool]:
        """Probe health + list_models para cada provider. Retorna {name: ok}."""
        ...
```

### 10.6 Esqueleto — `src/providers/llamacpp_provider.py`

```python
import httpx
from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse

class LlamaCppProvider(BaseProvider):
    """OpenAI-compatible. Soporta :8080 directo y :8000 atomic proxy."""

    def __init__(self, base_url: str = "http://localhost:8080", timeout_s: float = 300):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def chat(self, messages, tools=None, model=None, **kw) -> LLMResponse:
        # POST {base_url}/v1/chat/completions  {model, messages, tools, tool_choice}
        # Mapear a LLMResponse; parse reasoning_content si presente
        ...

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/health")
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        r = await self._client.get(f"{self.base_url}/v1/models")
        return [m["id"] for m in r.json().get("data", [])]
```

*(Analogos `ollama_provider.py` usa `POST /api/chat`; `cloud_provider.py` anade `Authorization: Bearer ${API_KEY}` via `AuthStyle`.)*

---

## 11. Integracion con Brain (portable, configurable)

### 11.1 No duplicar — Enlazar via NEXUS_BRAIN

```python
# src/brain/link.py
import os
from pathlib import Path

def brain_path() -> Path:
    return Path(os.environ.get("NEXUS_BRAIN", Path(__file__).parents[2] / "brain"))

# Uso:
#   cerebro.db      → brain_path() / "cerebro.db"        (brain_remember/recall)
#   nexus_memory.db → brain_path() / "nexus_memory.db"   (FTS5 observations)
#   knowledge_graph.db → brain_path() / "knowledge_graph.db"
# Si NEXUS_BRAIN apunta a otro directorio (ej. /ruta/a/otro/brain), Nexus v3
# lee/escribe el MISMO brain. Sin sync, sin duplicacion.
```

### 11.2 Capas de memoria (BRAIN_ARCHITECTURE.md)

```
HierarchicalMemory (3 tiers, forgetting curves, auto-promocion)
  → RAG Engine (nomic-embed-text 768d, chunk 800+100, cache MD5)
    → MultiSignalRetrieval (vector + BM25 + entities en paralelo)
      → SelfLearningLoop (cada 120s, judge + AdaptiveRouter)
```

Nexus v3 las consume via `src/brain/*.py` wrappers que delegan a `src/core/*` si `NEXUS_BRAIN` apunta a un brain externo, o usan implementacion local si es instalacion limpia.

---

## 12. Matriz de Trazabilidad — Que se absorbe de donde

| Modulo v3 | DeepSeek Cordis | Hermes | OpenCode | SuperNEXUS v2 | Otro |
|-----------|----------------|--------|----------|---------------|------|
| `session_log.py` | `packages/session` + `storage` | `trajectory.py` + `conversation_loop` | SQLite sessions + TUI | `session_manager.py` + `event_store.py` | Lethe (blocks) |
| `agent_loop.py` | `packages/core` + `hooks` | `conversation_loop.py` (3900 LOC) + `iteration_budget` + `error_classifier` | — | `agent_loop.py` (TDAO + ErrorAction) | OpenHands (ToM) |
| `tool_registry.py` | `skill` + `mcp` + `typert` | `tool_executor` + `toolsets` + `tool_guardrails` | — | `tool_registry.py` (DirectorToolRegistry) | ECC (layers) |
| `scope.py` | `guard` + `fs` | `file_safety.py` | Container pattern | `tool_guardrails.py` + `path_normalize.py` | — |
| `compaction.py` | `compaction` | `context_compressor` + `trajectory_compressor` | — | `context_compactor.py` (4-layer + metrics) | Lethe DMN |
| `provider_router.py` | `llm` | `credential_pool` + `model_metadata` | — | `provider_router.py` + `adaptive_router` + `vram_router` + `provider_base` | — |
| `hooks_engine.py` | `hooks` (CONTINUE/STOP/COMPACT/INJECT) | `shell_hooks.py` | — | `hooks_engine.py` (3-gate security) | — |
| `brain/*` | — | `memory_manager.py` | — | `BRAIN_ARCHITECTURE.md` (4 capas) | mem0/Engram/Lethe |

---

## 13. Roadmap de Implementacion

| Fase | Entregable | Criterio de done |
|------|------------|------------------|
| **0. Bootstrap** | `config.yaml` + `config.schema.json` + `src/core/provider_base.py` + `src/brain/link.py` | `python -m src.cli.main --check-config` verde; `brain_path()` resuelve brain externo si esta configurado |
| **1. Session** | `session_log.py` + `event_store.py` + `trajectory.py` + tests | CRUD + FTS + fork; WAL verificado con `PRAGMA journal_mode` |
| **2. Providers** | `ollama_provider.py` + `llamacpp_provider.py` + `cloud_provider.py` + `provider_router.py` | `auto_detect()` OK contra :11434/:8080/:9000; routing por hint |
| **3. Scope+Tools** | `scope.py` + `tool_registry.py` + `hooks_engine.py` + `error_classifier.py` | Guard bloquea `../../.env`; registry expone `to_openai_tools()` |
| **4. Compaction** | `compaction.py` (4 layers) | Metrics por capa; `reactive_compact` parsea `context_limit` desde error |
| **5. Agent Loop** | `agent_loop.py` + `iteration_budget.py` | TDAO loop E2E con mock provider; `LoopResult` con `steps` |
| **6. Brain link** | `hierarchical_memory.py` + `rag_engine.py` + `multi_signal.py` | Lee `cerebro.db` de v2 si presente; fallback local |
| **7. MCP+UI** | `mcp/bridge.py` + `ui/` | `FastMCP` bridge expone 38 tools; UI lista sesiones |
| **8. Hardening** | Audit 12-layer (ECC) + security scan | Sin wrapper-regression ni memory-pollution |

---

## 14. Referencias (arquitectura — implementacion de referencia interna)

- DeepSeek Harness Cordis: harness agéntico de referencia (50+ packages, `packages/AGENTS.md`)
- Hermes Agent: `agent/` (`conversation_loop.py`, `context_compressor.py`, `tool_executor.py`)
- Núcleo agéntico: `src/core/` (`agent_loop.py`, `tool_registry.py`, `context_compactor.py`, `provider_*`, `session_manager.py`)
- Mem0/Engram/Graphify/Lethe/CodeGraph: análisis comparativo de memorias agénticas (ver `docs/`)


---

*Blueprint generado por opencode (muse-spark) — 2026-09-03. No es implementacion; es contrato para construir.*
