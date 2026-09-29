"""
Model Registry — Unified catalog of ALL available models with capabilities.

Inspired by openakita/llm/capabilities.py (869 lines) and 01_CORE/nexus_director.py.
Provides intelligent model selection based on task requirements.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class Capability(Enum):
    CHAT = "chat"
    CODE = "code"
    REASONING = "reasoning"
    RESEARCH = "research"
    VISION = "vision"
    CREATIVE = "creative"
    ANALYSIS = "analysis"
    FAST = "fast"
    EMBEDDING = "embedding"
    TOOLS = "tools"


@dataclass
class ModelInfo:
    id: str
    name: str
    provider: str  # "ollama" | "opencode-zen"
    capabilities: list[Capability]
    context_window: int = 8192
    speed_tps: float = 25.0
    quality: float = 5.0  # 1-10
    cost: float = 0.0  # per 1k tokens
    description: str = ""

    def supports(self, cap: Capability) -> bool:
        return cap in self.capabilities


# ── Unified Model Catalog ────────────────────────────────────────────────────

MODEL_CATALOG: list[ModelInfo] = [
    # === OpenCode Zen (Cloud, Free) ===
    ModelInfo(
        id="nemotron-3-ultra-free",
        name="Nemotron 3 Ultra Free",
        provider="opencode-zen",
        capabilities=[Capability.CHAT, Capability.REASONING, Capability.RESEARCH, Capability.ANALYSIS, Capability.TOOLS],
        context_window=128000,
        speed_tps=50,
        quality=8,
        cost=0,
        description="Modelo principal free. Analisis profundo, razonamiento, proposito general.",
    ),
    ModelInfo(
        id="deepseek-v4-flash-free",
        name="DeepSeek V4 Flash Free (DOWN)",
        provider="opencode-zen",
        capabilities=[Capability.CHAT, Capability.REASONING, Capability.RESEARCH, Capability.TOOLS],
        context_window=128000,
        speed_tps=60,
        quality=8,
        cost=0,
        description="DESPERDICADO - No disponible. Usar nemotron-3-ultra-free.",
    ),
    ModelInfo(
        id="mimo-v2.5-free",
        name="MiMo V2.5 Free",
        provider="opencode-zen",
        capabilities=[Capability.CHAT, Capability.REASONING, Capability.VISION, Capability.CODE, Capability.TOOLS],
        context_window=128000,
        speed_tps=40,
        quality=9,
        cost=0,
        description="Vision + codigo + razonamiento. UNICO con vision. Ideal para imagenes, screenshots.",
    ),
    ModelInfo(
        id="muse-spark-1.2-contributor-free",
        name="Muse Spark 1.2 Contributor Free",
        provider="opencode-zen",
        capabilities=[Capability.CHAT, Capability.FAST, Capability.CREATIVE, Capability.TOOLS],
        context_window=128000,
        speed_tps=70,
        quality=7,
        cost=0,
        description="Rapido, creativo, simple. Para tareas ligeras, resumenes, clasificacion.",
    ),
    ModelInfo(
        id="hy3-free",
        name="Hy3 Free",
        provider="opencode-zen",
        capabilities=[Capability.CHAT, Capability.REASONING, Capability.TOOLS],
        context_window=128000,
        speed_tps=55,
        quality=8,
        cost=0,
        description="Rapido, proposito general. Confirmado en catalogo opencode (2026-08-14).",
    ),
    ModelInfo(
        id="nemotron-3.5-lightning-free",
        name="Nemotron 3.5 Lightning Free",
        provider="opencode-zen",
        capabilities=[Capability.CHAT, Capability.REASONING, Capability.TOOLS],
        context_window=128000,
        speed_tps=65,
        quality=8,
        cost=0,
        description="Rapido, razonamiento ligero. Confirmado en catalogo opencode (2026-08-14).",
    ),
    ModelInfo(
        id="laguna-s-2.1-free",
        name="Laguna S 2.1 Free",
        provider="opencode-zen",
        capabilities=[Capability.CHAT, Capability.REASONING],
        context_window=128000,
        speed_tps=45,
        quality=7,
        cost=0,
        description="Modelo simple (responde pero no calcula bien). Confirmado en catalogo opencode (2026-08-14).",
    ),
    ModelInfo(
        id="north-mini-code-free",
        name="North Mini Code Free",
        provider="opencode-zen",
        capabilities=[Capability.CHAT, Capability.CODE, Capability.TOOLS],
        context_window=32000,
        speed_tps=70,
        quality=7,
        cost=0,
        description="Especializado en codigo. Rapido para programacion.",
    ),
    ModelInfo(
        id="ling-3.0-flash-fin-free",
        name="Ling 3.0 Flash Fin Free",
        provider="opencode",
        capabilities=[Capability.CHAT, Capability.FAST, Capability.REASONING, Capability.TOOLS],
        context_window=128000,
        speed_tps=60,
        quality=8,
        cost=0,
        description="Flash financiero, rapido y razonamiento ligero. Via opencode CLI (no zen directo).",
    ),

    # === Llama.cpp (Local, Free, D:\models <8GB) - auto swap, no VRAM si no se usa ===
    ModelInfo(
        id="nexus-director-v6",
        name="Nexus Director v6 (Llama.cpp Q8_0 4.3GB)",
        provider="llama",
        capabilities=[Capability.CHAT, Capability.REASONING, Capability.ANALYSIS, Capability.TOOLS],
        context_window=40960,
        speed_tps=30,
        quality=9,
        cost=0,
        description="Director/orquestador residente :8080. D:\\ias\\...\\nexus-director-v6-Q8_0.gguf",
    ),
    ModelInfo(
        id="qwen3.5-9b",
        name="Qwen 3.5 9B (Llama.cpp Q4 5.29GB)",
        provider="llama",
        capabilities=[Capability.CHAT, Capability.CODE, Capability.CREATIVE, Capability.REASONING],
        context_window=131072,
        speed_tps=35,
        quality=9,
        cost=0,
        description="Codigo y chat general. D:\\models\\Qwen3.5-9B\\Qwen3.5-9B-Q4_K_M.gguf - swap 30s",
    ),
    ModelInfo(
        id="deepseek-r1:8b",
        name="DeepSeek R1 8B (Llama.cpp Q4 4.87GB)",
        provider="llama",
        capabilities=[Capability.CHAT, Capability.REASONING, Capability.RESEARCH],
        context_window=32000,
        speed_tps=20,
        quality=9,
        cost=0,
        description="Razonamiento profundo. D:\\models\\library-deepseek-r1-8b.gguf",
    ),
    ModelInfo(
        id="qwen3-vl-4b",
        name="Qwen3 VL 4B (Llama.cpp Q4 2.33GB + mmproj)",
        provider="llama",
        capabilities=[Capability.CHAT, Capability.VISION],
        context_window=32000,
        speed_tps=25,
        quality=8,
        cost=0,
        description="Vision Qwen3. D:\\models\\Qwen3-VL-4B - swap con mmproj",
    ),
    ModelInfo(
        id="janus-pro-7b",
        name="Janus Pro 7B Vision (Llama.cpp Q4 3.93GB)",
        provider="llama",
        capabilities=[Capability.CHAT, Capability.VISION],
        context_window=32000,
        speed_tps=20,
        quality=8,
        cost=0,
        description="Vision DeepSeek Janus. D:\\models\\Janus-Pro-7B-Q4",
    ),
    ModelInfo(
        id="omnicoder-2-9b",
        name="Omnicoder 2 9B (Llama.cpp Q4 5.34GB)",
        provider="llama",
        capabilities=[Capability.CHAT, Capability.CODE],
        context_window=32000,
        speed_tps=30,
        quality=9,
        cost=0,
        description="Codigo especializado. D:\\models\\carstenuhlig-omnicoder-2-9b",
    ),
    ModelInfo(
        id="nemotron-3-nano:4b",
        name="Nemotron 3 Nano 4B (Llama.cpp Q4 2.64GB)",
        provider="llama",
        capabilities=[Capability.CHAT, Capability.ANALYSIS, Capability.FAST],
        context_window=8192,
        speed_tps=60,
        quality=5,
        cost=0,
        description="Rapido lightweight. D:\\models\\library-nemotron-3-nano-4b.gguf",
    ),
    ModelInfo(
        id="nomic-embed-text",
        name="Nomic Embed Text (Llama.cpp Q4 0.26GB)",
        provider="llama",
        capabilities=[Capability.EMBEDDING],
        context_window=8192,
        speed_tps=100,
        quality=8,
        cost=0,
        description="Embeddings RAG. D:\\models\\library-nomic-embed-text-latest.gguf - siempre CPU",
    ),
]


# ── Task Type Classification ─────────────────────────────────────────────────

class TaskType(Enum):
    CODE = "code"
    RESEARCH = "research"
    REASONING = "reasoning"
    VISION = "vision"
    CREATIVE = "creative"
    ANALYSIS = "analysis"
    CHAT = "chat"
    FAST = "fast"


# Keywords that map to task types
TASK_KEYWORDS: dict[TaskType, list[str]] = {
    TaskType.CODE: [
        "programa", "codigo", "code", "python", "javascript", "typescript", "react",
        "bug", "debug", "refactor", "implementar", "clase", "funcion", "api",
        "docker", "server", "infra", "deploy", "git", "npm", "pip",
    ],
    TaskType.RESEARCH: [
        "investiga", "research", "busca", "web", "paper", "estudia", "aprende",
        "documentacion", "tutorial", "que es", "como funciona", "explica",
        "noticias", "actualidad", "leyes", "normativa",
    ],
    TaskType.REASONING: [
        "razona", "piensa", "analiza profundamente", "por que", "causa",
        "solucion", "problema", "evalua", "compara", "decide", "estrategia",
    ],
    TaskType.VISION: [
        "imagen", "captura", "screenshot", "foto", "video", "ocr",
        "mirá", "ve这张", "que ves", "describe la imagen",
    ],
    TaskType.CREATIVE: [
        "escribe", "cuento", "historia", "blog", "articulo", "contenido",
        "creativo", "narrativa", "copy", "marketing", "redes sociales",
    ],
    TaskType.ANALYSIS: [
        "analisis", "datos", "metricas", "estadistica", "reporte",
        "resumen", "evalua", "mide", "compara",
    ],
    TaskType.FAST: [
        "rapido", "breve", "corto", "resumí", "dame un ejemplo",
    ],
}


def classify_task_type(task: str) -> TaskType:
    """Classify a task into a TaskType based on keywords."""
    lower = task.lower()
    scores: dict[TaskType, int] = {t: 0 for t in TaskType}

    for task_type, keywords in TASK_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                scores[task_type] += 1

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return TaskType.CHAT
    return best


# ── Model Selection ──────────────────────────────────────────────────────────

# Mapping: TaskType → preferred capabilities (in priority order)
TASK_PREFERRED_CAPS: dict[TaskType, list[Capability]] = {
    TaskType.CODE: [Capability.CODE, Capability.TOOLS, Capability.CHAT],
    TaskType.RESEARCH: [Capability.RESEARCH, Capability.REASONING, Capability.TOOLS, Capability.CHAT],
    TaskType.REASONING: [Capability.REASONING, Capability.ANALYSIS, Capability.CHAT],
    TaskType.VISION: [Capability.VISION, Capability.CHAT],
    TaskType.CREATIVE: [Capability.CREATIVE, Capability.CHAT],
    TaskType.ANALYSIS: [Capability.ANALYSIS, Capability.REASONING, Capability.CHAT],
    TaskType.CHAT: [Capability.CHAT],
    TaskType.FAST: [Capability.FAST, Capability.CHAT],
}


def select_model(
    task: str,
    task_type: Optional[TaskType] = None,
    prefer_local: bool = False,
    require_vision: bool = False,
) -> ModelInfo:
    """Select the best model for a given task.

    Args:
        task: The task description
        task_type: Override task type classification
        prefer_local: If True, prefer Ollama models (lower latency)
        require_vision: If True, require vision capability
    """
    if task_type is None:
        task_type = classify_task_type(task)

    required_caps = [c for c in TASK_PREFERRED_CAPS.get(task_type, [Capability.CHAT])]
    if require_vision:
        required_caps = [Capability.VISION] + required_caps

    # FIX 2026-08-13: solo puntuar modelos REALMENTE disponibles
    available_ids = get_available_model_ids()

    # Score each model
    scored: list[tuple[float, ModelInfo]] = []
    for model in MODEL_CATALOG:
        if prefer_local and model.provider not in ("ollama", "llama"):
            continue

        # FIX: skip modelos no instalados / sin key
        if available_ids and model.id not in available_ids:
            continue

        # Check required capabilities
        has_required = all(model.supports(c) for c in required_caps[:2])
        if not has_required:
            continue

        # Score: capability match + quality + speed
        cap_match = sum(1 for c in required_caps if model.supports(c))
        score = (
            cap_match * 3.0           # capability match weight
            + model.quality * 0.5     # quality weight
            + model.speed_tps * 0.01  # speed weight (minor)
            - model.cost * 100        # cost penalty
        )
        scored.append((score, model))

    if not scored:
        # FIX 2026-08-13: fallback SOLO entre modelos realmente disponibles
        for m in MODEL_CATALOG:
            if not prefer_local or m.provider in ("ollama", "llama"):
                if not available_ids or m.id in available_ids:
                    return m
        return MODEL_CATALOG[0]

    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1]
    logger.info(f"Model selected: {best.id} for task_type={task_type.value} (score={scored[0][0]:.1f})")
    return best


def get_model_by_id(model_id: str) -> Optional[ModelInfo]:
    """Get a model by its ID."""
    for m in MODEL_CATALOG:
        if m.id == model_id:
            return m
    return None


# ── Disponibilidad real (FIX 2026-08-13) ─────────────────────────────────────
# El director elegía modelos del catálogo sin verificar que existieran en
# Ollama local o que hubiera key de Zen → el provider fallaba y el agente
# devolvía vacío. Ahora consultamos los modelos REALMENTE instalados.

_available_cache: Optional[set] = None
_available_cache_ts: float = 0.0
_AVAILABLE_TTL = 60.0  # 1 min cache

import time as _time


def _ollama_installed_models() -> set:
    """Consulta Ollama /api/tags (timeout corto, no bloqueante)."""
    try:
        import httpx
        url = "http://127.0.0.1:11434/api/tags"
        r = httpx.get(url, timeout=2.5)
        if r.status_code == 200:
            data = r.json()
            return {m.get("name", "") for m in data.get("models", [])}
    except Exception:
        pass
    return set()

def _llama_installed_models() -> set:
    """GGUF en D:\\models (llama.cpp) - no ocupan VRAM hasta cargar."""
    import pathlib
    ids = set()
    # mapeo id -> path GGUF (todos <8GB)
    mapping = {
        "nexus-director-v6": r"D:\ias\proyectos\supernexus-v2\models\nexus-director-v6\nexus-director-v6-Q8_0.gguf",
        "qwen3.5-9b": r"D:\models\Qwen3.5-9B\Qwen3.5-9B-Q4_K_M.gguf",
        "deepseek-r1:8b": r"D:\models\ollama_export\library-deepseek-r1-8b.gguf",
        "qwen3-vl-4b": r"D:\models\Qwen3-VL-8B\Qwen3VL-8B-Instruct-Q4_K_M.gguf",
        "janus-pro-7b": r"D:\models\Janus-Pro-7B-Q4\Janus-Pro-7B.Q4_K_M.gguf",
        "omnicoder-2-9b": r"D:\models\ollama_export\carstenuhlig-omnicoder-2-9b-q4_k_m.gguf",
        "nemotron-3-nano:4b": r"D:\models\ollama_export\library-nemotron-3-nano-4b.gguf",
        "nomic-embed-text": r"D:\models\ollama_export\library-nomic-embed-text-latest.gguf",
    }
    for mid, p in mapping.items():
        if pathlib.Path(p).exists():
            ids.add(mid)
    return ids


def _zen_key_available() -> bool:
    """Hay key de OpenCode Zen? (env, cloud_providers.json o auth.json de opencode CLI)"""
    import os
    if os.environ.get("OPENCODE_API_KEY"):
        return True
    try:
        cp_path = os.path.join(os.path.expanduser("~"), ".nexus", "cloud_providers.json")
        if os.path.exists(cp_path):
            with open(cp_path, "r", encoding="utf-8") as f:
                import json
                _cp = json.load(f)
            _iter = _cp.values() if isinstance(_cp, dict) else _cp
            for v in _iter:
                if isinstance(v, dict) and v.get("api_key"):
                    return True
    except Exception:
        pass
    # Key de la CLI opencode (auth.json) — la fuente real usada por el backend
    try:
        import json as _json
        for p in [
            os.path.expanduser("~/.local/share/opencode/auth.json"),
            os.path.expanduser("~/.opencode/auth.json"),
            os.path.expanduser("~/.config/opencode/auth.json"),
            os.path.expanduser("~/AppData/Local/opencode/auth.json"),
        ]:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = _json.load(f)
                # estructura: {"opencode": {"type": "api", "key": "sk-..."}}
                for v in d.values():
                    if isinstance(v, dict) and v.get("key"):
                        return True
                break
    except Exception:
        pass
    return False


def get_available_model_ids() -> set:
    """IDs de modelos REALMENTE disponibles (Ollama instalados + Zen con key)."""
    global _available_cache, _available_cache_ts
    now = _time.time()
    if _available_cache is not None and (now - _available_cache_ts) < _AVAILABLE_TTL:
        return _available_cache

    ollama = _ollama_installed_models()
    zen_ok = _zen_key_available()

    llama = _llama_installed_models()
    ids: set = set()
    for m in MODEL_CATALOG:
        if m.provider == "ollama":
            if m.id in ollama:
                ids.add(m.id)
        elif m.provider == "llama":
            if m.id in llama:
                ids.add(m.id)
        elif m.provider == "opencode-zen":
            if zen_ok:
                ids.add(m.id)

    _available_cache = ids
    _available_cache_ts = now
    return ids


def list_models(provider: Optional[str] = None) -> list[ModelInfo]:
    """List all models, optionally filtered by provider."""
    if provider:
        return [m for m in MODEL_CATALOG if m.provider == provider]
    return list(MODEL_CATALOG)
