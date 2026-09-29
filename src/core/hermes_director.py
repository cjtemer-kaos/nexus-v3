"""
HermesDirector — Integración del Director de SuperNEXUS en Hermes

Patrón: Task Classification → Model Selection → Execution

Basado en:
- DirectorNexus v2 (SuperNEXUS)
- AdaptiveRouter (Thompson Sampling)
- RoutingBrain (4 fases de clasificación)
"""
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Modelos disponibles (Hermes providers) ──────────────────────────────

MODELS = {
    # OpenCode Zen (pay-as-you-go)
    "mimo-v2.5-free": {"provider": "opencode", "tier": "free", "strengths": ["general", "chat"]},
    "deepseek-v4-flash-free": {"provider": "opencode", "tier": "free", "strengths": ["code", "analysis"]},
    "deepseek-v4-pro": {"provider": "opencode", "tier": "paid", "strengths": ["code", "reasoning"]},
    "gpt-5.5": {"provider": "opencode", "tier": "paid", "strengths": ["general", "creative"]},
    "gpt-5.5-pro": {"provider": "opencode", "tier": "paid", "strengths": ["reasoning", "analysis"]},
    "claude-fable-5": {"provider": "opencode", "tier": "paid", "strengths": ["code", "reasoning"]},
    "claude-opus-4-8": {"provider": "opencode", "tier": "paid", "strengths": ["reasoning", "analysis"]},
    "claude-sonnet-4-6": {"provider": "opencode", "tier": "paid", "strengths": ["code", "general"]},
    "gemini-3.5-flash": {"provider": "opencode", "tier": "paid", "strengths": ["general", "fast"]},
    "qwen3.7-plus": {"provider": "opencode", "tier": "paid", "strengths": ["code", "multilingual"]},
    "glm-5.2": {"provider": "opencode", "tier": "paid", "strengths": ["general", "reasoning"]},
    "minimax-m3": {"provider": "opencode", "tier": "paid", "strengths": ["general", "creative"]},
    
    # OpenCode Go (subscription)
    "mimo-v2.5-pro": {"provider": "opencode-go", "tier": "subscription", "strengths": ["reasoning", "code"]},
    "mimo-v2-pro": {"provider": "opencode-go", "tier": "subscription", "strengths": ["general", "fast"]},
    "deepseek-v4-pro-go": {"provider": "opencode-go", "tier": "subscription", "strengths": ["code", "reasoning"]},
    "qwen3.7-max": {"provider": "opencode-go", "tier": "subscription", "strengths": ["code", "analysis"]},
    "glm-5.1-go": {"provider": "opencode-go", "tier": "subscription", "strengths": ["general", "multilingual"]},
    "minimax-m2.7-go": {"provider": "opencode-go", "tier": "subscription", "strengths": ["creative", "general"]},
    
    # SuperNEXUS Ollama (local)
    "nexus-director-v6": {"provider": "ollama", "tier": "local", "vram": 4.3, "strengths": ["routing", "classification"]},
    "omnicoder-2-9b": {"provider": "ollama", "tier": "local", "vram": 5.7, "strengths": ["code", "engineering"]},
    "qwen3.5:9b": {"provider": "ollama", "tier": "local", "vram": 6.6, "strengths": ["general", "chat"]},
    "deepseek-r1:8b": {"provider": "ollama", "tier": "local", "vram": 5.2, "strengths": ["reasoning", "analysis"]},
    "qwen2.5-coder:7b": {"provider": "ollama", "tier": "local", "vram": 4.7, "strengths": ["code"]},
    "qwen2.5vl:7b": {"provider": "ollama", "tier": "local", "vram": 6.0, "strengths": ["vision", "multimodal"]},
    "gemma4:12b": {"provider": "ollama", "tier": "local", "vram": 7.6, "strengths": ["vision", "creative"]},
    "nemotron-3-nano:4b": {"provider": "ollama", "tier": "local", "vram": 2.8, "strengths": ["fast", "analysis"]},
    "qwen2.5:0.5b": {"provider": "ollama", "tier": "local", "vram": 0.4, "strengths": ["fast", "summary"]},
    "nomic-embed-text": {"provider": "ollama", "tier": "local", "vram": 0.3, "strengths": ["embeddings"]},
}


# ── Task Types (from SuperNEXUS RoutingBrain) ──────────────────────────

TASK_TYPES = {
    "code": {
        "keywords": ["code", "function", "class", "debug", "fix", "implement", "refactor", "python", "javascript", "typescript"],
        "preferred_tier": "paid",
        "preferred_strengths": ["code"],
    },
    "reasoning": {
        "keywords": ["analyze", "reason", "think", "evaluate", "compare", "decide", "strategy"],
        "preferred_tier": "paid",
        "preferred_strengths": ["reasoning"],
    },
    "creative": {
        "keywords": ["write", "create", "design", "story", "poem", "creative", "imagine"],
        "preferred_tier": "paid",
        "preferred_strengths": ["creative"],
    },
    "general": {
        "keywords": ["help", "explain", "what", "how", "why", "tell", "describe"],
        "preferred_tier": "free",
        "preferred_strengths": ["general", "chat"],
    },
    "fast": {
        "keywords": ["quick", "fast", "simple", "brief", "short", "summary"],
        "preferred_tier": "free",
        "preferred_strengths": ["fast"],
    },
    "analysis": {
        "keywords": ["analyze", "data", "statistics", "report", "metrics", "performance"],
        "preferred_tier": "paid",
        "preferred_strengths": ["analysis"],
    },
    "vision": {
        "keywords": ["image", "screenshot", "diagram", "visual", "see", "look", "describe image"],
        "preferred_tier": "local",
        "preferred_strengths": ["vision"],
    },
}


# ── Thompson Sampling (from SuperNEXUS AdaptiveRouter) ──────────────────

@dataclass
class ModelStats:
    """Estadísticas de rendimiento de un modelo."""
    alpha: float = 1.0
    beta: float = 1.0
    total_calls: int = 0
    total_latency_ms: float = 0.0
    success_count: int = 0
    
    @property
    def success_rate(self) -> float:
        return self.success_count / max(self.total_calls, 1)
    
    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.total_calls, 1)
    
    def sample(self) -> float:
        """Thompson Sampling: extrae una muestra de la distribución beta."""
        return random.betavariate(self.alpha, self.beta)
    
    def update(self, reward: float, latency_ms: float = 0):
        """Actualiza stats con resultado de ejecución."""
        self.total_calls += 1
        self.total_latency_ms += latency_ms
        if reward > 0.5:
            self.success_count += 1
            self.alpha += reward
        else:
            self.beta += (1.0 - reward)


class HermesDirector:
    """
    Director de Hermes — selecciona el mejor modelo para cada tarea.
    
    Patrón:
    1. Clasificar tarea (keywords + LLM fallback)
    2. Seleccionar modelo (Thompson Sampling)
    3. Ejecutar y aprender
    """
    
    def __init__(self):
        self._stats: Dict[str, ModelStats] = {}
        self._task_history: List[Dict] = []
        self._load_stats()
    
    def _load_stats(self):
        """Carga stats desde disco."""
        stats_file = os.path.expanduser("~/.hermes/director_stats.json")
        if os.path.exists(stats_file):
            try:
                with open(stats_file, "r") as f:
                    data = json.load(f)
                    for model, stats in data.items():
                        self._stats[model] = ModelStats(**stats)
            except Exception:
                pass
    
    def _save_stats(self):
        """Guarda stats a disco."""
        stats_file = os.path.expanduser("~/.hermes/director_stats.json")
        os.makedirs(os.path.dirname(stats_file), exist_ok=True)
        try:
            data = {model: {
                "alpha": s.alpha,
                "beta": s.beta,
                "total_calls": s.total_calls,
                "total_latency_ms": s.total_latency_ms,
                "success_count": s.success_count,
            } for model, s in self._stats.items()}
            with open(stats_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save director stats: {e}")
    
    def classify_task(self, task: str) -> str:
        """
        Clasifica una tarea en un tipo.
        
        Fases (igual que SuperNEXUS):
        1. Multi-word patterns
        2. Single keyword matching
        3. Default to "general"
        """
        task_lower = task.lower()
        
        # Phase 1: Multi-word patterns (more specific)
        multi_word_patterns = {
            "debug this": "code",
            "fix this bug": "code",
            "write a function": "code",
            "implement this": "code",
            "refactor this": "code",
            "analyze this": "analysis",
            "compare these": "analysis",
            "write a story": "creative",
            "create a design": "creative",
            "explain how": "general",
            "what is": "general",
            "describe this image": "vision",
            "look at this": "vision",
            "quick summary": "fast",
            "brief answer": "fast",
        }
        
        for pattern, task_type in multi_word_patterns.items():
            if pattern in task_lower:
                return task_type
        
        # Phase 2: Single keyword matching
        for task_type, config in TASK_TYPES.items():
            for keyword in config["keywords"]:
                if keyword in task_lower:
                    return task_type
        
        # Phase 3: Default
        return "general"
    
    def select_model(self, task_type: str, tier_preference: str = "auto") -> str:
        """
        Selecciona el mejor modelo usando Thompson Sampling.
        
        Args:
            task_type: Tipo de tarea (code, reasoning, creative, etc.)
            tier_preference: "free", "paid", "local", o "auto"
        
        Returns:
            Nombre del modelo seleccionado
        """
        # Filter models by tier preference
        if tier_preference == "auto":
            # Auto: try free first, then paid
            candidates = [
                (name, info) for name, info in MODELS.items()
                if info["tier"] == "free"
            ]
            if not candidates:
                candidates = [
                    (name, info) for name, info in MODELS.items()
                    if info["tier"] == "paid"
                ]
        else:
            candidates = [
                (name, info) for name, info in MODELS.items()
                if info["tier"] == tier_preference
            ]
        
        if not candidates:
            return "mimo-v2.5-free"  # Fallback
        
        # Filter by task type strengths
        task_config = TASK_TYPES.get(task_type, TASK_TYPES["general"])
        preferred_strengths = set(task_config["preferred_strengths"])
        
        # Score each candidate
        scored = []
        for name, info in candidates:
            # Base score from Thompson Sampling
            stats = self._stats.get(name, ModelStats())
            thompson_score = stats.sample()
            
            # Bonus for matching strengths
            model_strengths = set(info["strengths"])
            strength_bonus = 0.3 if model_strengths & preferred_strengths else 0
            
            # Penalty for high latency (normalize to 0-1)
            latency_penalty = min(stats.avg_latency_ms / 5000, 0.2)
            
            final_score = thompson_score + strength_bonus - latency_penalty
            scored.append((name, final_score))
        
        # Select best
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]
    
    def record_outcome(self, model: str, success: bool, latency_ms: float = 0):
        """
        Registra resultado de ejecución para aprendizaje.
        
        Args:
            model: Nombre del modelo usado
            success: Si la tarea fue exitosa
            latency_ms: Latencia en milisegundos
        """
        if model not in self._stats:
            self._stats[model] = ModelStats()
        
        reward = 1.0 if success else 0.0
        self._stats[model].update(reward, latency_ms)
        self._save_stats()
        
        logger.info(f"Director: {model} → success={success}, latency={latency_ms}ms")
    
    def get_recommendation(self, task: str) -> Dict[str, Any]:
        """
        Recomienda modelo para una tarea.
        
        Returns:
            Dict con task_type, model, confidence, reasoning
        """
        task_type = self.classify_task(task)
        model = self.select_model(task_type)
        stats = self._stats.get(model, ModelStats())
        
        return {
            "task": task,
            "task_type": task_type,
            "model": model,
            "provider": MODELS.get(model, {}).get("provider", "unknown"),
            "confidence": stats.success_rate if stats.total_calls > 0 else 0.5,
            "reasoning": f"Task classified as '{task_type}', model '{model}' selected via Thompson Sampling",
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas del director."""
        return {
            "total_models": len(self._stats),
            "total_calls": sum(s.total_calls for s in self._stats.values()),
            "models": {
                name: {
                    "calls": s.total_calls,
                    "success_rate": round(s.success_rate, 2),
                    "avg_latency_ms": round(s.avg_latency_ms, 1),
                }
                for name, s in sorted(self._stats.items(), key=lambda x: x[1].total_calls, reverse=True)
            }
        }


# Singleton
_director: Optional[HermesDirector] = None


def get_director() -> HermesDirector:
    """Get or create Director instance."""
    global _director
    if _director is None:
        _director = HermesDirector()
    return _director
