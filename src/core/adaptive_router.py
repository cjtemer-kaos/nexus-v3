from __future__ import annotations
from src.core.environment import data_home

import json
import logging
import random
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from src.core.actor_base import Actor, ActorMessage, ActorResult, MessageIntent, classify_intent, route_by_content
from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_ROUTER_STORAGE = data_home() / "router_state.json"


@dataclass
class ModelOutcome:
    alpha: float = 1.0
    beta: float = 1.0
    total_calls: int = 0
    total_latency_ms: float = 0.0
    total_cost: float = 0.0
    success_count: int = 0
    last_used: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success_count / max(self.total_calls, 1)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.total_calls, 1)

    def sample(self) -> float:
        return random.betavariate(self.alpha, self.beta)

    def update(self, reward: float, latency_ms: float = 0, cost: float = 0):
        self.total_calls += 1
        self.total_latency_ms += latency_ms
        self.total_cost += cost
        self.last_used = time.time()
        if reward > 0.5:
            self.success_count += 1
            self.alpha += reward
        else:
            self.beta += (1.0 - reward)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ModelOutcome:
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items()})


@dataclass
class ModelEntry:
    name: str
    provider: str = "ollama"
    context_window: int = 8192
    cost_per_1k: float = 0.0
    speed_tps: float = 25.0
    capabilities: list[str] = field(default_factory=lambda: ["chat"])
    quality_rating: float = 5.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ModelEntry:
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items()})


DEFAULT_CATALOG: dict[str, ModelEntry] = {
    "nexus-coder": ModelEntry("nexus-coder", "ollama", 8192, 0, 25, ["chat", "code", "tools"], 7),
    "qwen2.5:0.5b": ModelEntry("qwen2.5:0.5b", "ollama", 32768, 0, 80, ["chat"], 3),
    "qwen2.5-coder:7b": ModelEntry("qwen3.5-9b", "llama", 131072, 0, 35, ["chat", "code", "creative"], 8),
    "deepseek-r1:8b": ModelEntry("deepseek-r1:8b", "ollama", 131072, 0, 20, ["chat", "reasoning", "research"], 9),
    "nemotron-3-nano:4b": ModelEntry("nemotron-3-nano:4b", "ollama", 8192, 0, 50, ["chat", "analysis"], 5),
    "qwen2.5vl:7b": ModelEntry("qwen2.5vl:7b", "ollama", 32768, 0, 25, ["chat", "vision"], 6),
    "nomic-embed-text": ModelEntry("nomic-embed-text", "ollama", 8192, 0, 100, ["embedding"], 8),
}


class ThompsonSampler:
    def __init__(self, catalog: dict[str, ModelEntry] | None = None):
        import threading
        self._catalog = catalog if catalog is not None else dict(DEFAULT_CATALOG)
        self._outcomes: dict[str, ModelOutcome] = {}
        self._lock = threading.Lock()
        self._save_count = 0  # Throttle saves
        self._load()

    def get_candidates(self, task: str, required_context: int = 0) -> list[ModelEntry]:
        lower = task.lower()
        candidates = list(self._catalog.values())

        if required_context > 0:
            candidates = [m for m in candidates if m.context_window >= required_context]

        keywords_vision = ["imagen", "captura", "screenshot", "vision", "ocr"]
        keywords_code = ["código", "programar", "implementar", "bug", "refactor", "python", "typescript"]
        keywords_reasoning = ["investigar", "analizar", "research", "por qué", "cómo funciona", "explica"]
        keywords_creative = ["creativo", "escribir", "narrativa", "blog", "copy", "contenido"]

        if any(kw in lower for kw in keywords_vision):
            candidates = [m for m in candidates if "vision" in m.capabilities]
        elif any(kw in lower for kw in keywords_code):
            candidates = [m for m in candidates if "code" in m.capabilities]
        elif any(kw in lower for kw in keywords_reasoning):
            candidates = [m for m in candidates if "reasoning" in m.capabilities or "research" in m.capabilities]
        elif any(kw in lower for kw in keywords_creative):
            candidates = [m for m in candidates if "creative" in m.capabilities]

        if not candidates:
            candidates = list(self._catalog.values())

        candidates = [m for m in candidates if m.name != "nomic-embed-text"]
        return candidates

    def select(self, task: str, required_context: int = 0,
               prefer_speed: bool = False, prefer_quality: bool = False) -> str:
        candidates = self.get_candidates(task, required_context)
        if not candidates:
            return "qwen3.5-9b"
        if len(candidates) == 1:
            return candidates[0].name

        best_score = -float("inf")
        best_model = candidates[0].name

        for entry in candidates:
            outcome = self._outcomes.get(entry.name)
            if outcome and outcome.total_calls > 0:
                sample = outcome.sample()
            else:
                sample = entry.quality_rating / 10.0

            cost_penalty = min(entry.cost_per_1k / 10.0, 0.3) if entry.cost_per_1k > 0 else 0.0
            speed_bonus = min(entry.speed_tps / 100.0, 0.2) if prefer_speed else 0.0
            quality_bonus = entry.quality_rating / 20.0 if prefer_quality else 0.0
            score = sample - cost_penalty + speed_bonus + quality_bonus

            total_calls = outcome.total_calls if outcome else 0
            if total_calls < 5:
                score += (5 - total_calls) * 0.02

            if score > best_score:
                best_score = score
                best_model = entry.name

        return best_model

    def record_outcome(self, model: str, reward: float, latency_ms: float = 0, cost: float = 0):
        with self._lock:
            if model not in self._outcomes:
                self._outcomes[model] = ModelOutcome()
            self._outcomes[model].update(reward, latency_ms, cost)
            self._save_count += 1
            # Throttle: save every 10 outcomes instead of every single one
            if self._save_count % 10 == 0:
                self._save()

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                name: {
                    "calls": o.total_calls,
                    "success_rate": round(o.success_rate, 3),
                    "avg_latency_ms": round(o.avg_latency_ms, 1),
                    "alpha": round(o.alpha, 2),
                    "beta": round(o.beta, 2),
                }
                for name, o in self._outcomes.items()
            }

    def _save(self):
        try:
            data = {
                "outcomes": {k: v.to_dict() for k, v in self._outcomes.items()},
                "catalog": {k: v.to_dict() for k, v in self._catalog.items()},
                "updated_at": time.time(),
            }
            _ROUTER_STORAGE.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_ROUTER_STORAGE, data)
        except Exception as e:
            logger.debug("Router state save failed: %s", e)

    def _load(self):
        try:
            if _ROUTER_STORAGE.exists():
                with open(_ROUTER_STORAGE) as f:
                    data = json.load(f)
                for name, d in data.get("outcomes", {}).items():
                    self._outcomes[name] = ModelOutcome.from_dict(d)
                for name, d in data.get("catalog", {}).items():
                    if name in self._catalog:
                        self._catalog[name] = ModelEntry.from_dict(d)
                logger.info("Loaded router state: %d models tracked", len(self._outcomes))
        except Exception as e:
            logger.debug("Router state load failed: %s", e)


class AdaptiveRouter:
    def __init__(self, sampler: ThompsonSampler | None = None):
        self._sampler = sampler or ThompsonSampler()

    def select_model(self, task: str, required_context: int = 0,
                     prefer_speed: bool = False, prefer_quality: bool = False) -> str:
        return self._sampler.select(task, required_context, prefer_speed, prefer_quality)

    def record_result(self, model: str, success: bool, quality_score: float = 0.5,
                      latency_ms: float = 0, cost: float = 0):
        reward = max(0.0, min(1.0, (1.0 if success else 0.0) * 0.5 + quality_score * 0.5))
        self._sampler.record_outcome(model, reward, latency_ms, cost)

    def get_stats(self) -> dict:
        return self._sampler.get_stats()

    def get_candidates(self, task: str) -> list[dict]:
        return [
            {"name": e.name, "provider": e.provider, "context_window": e.context_window,
             "speed_tps": e.speed_tps, "quality_rating": e.quality_rating,
             "capabilities": e.capabilities}
            for e in self._sampler.get_candidates(task)
        ]


class AdaptiveRouterActor(Actor):
    name = "adaptive_router"

    def __init__(self, adaptive_router: AdaptiveRouter, system: Any, actor_id: str = ""):
        super().__init__(actor_id=actor_id)
        self._router = adaptive_router
        self._system = system

    async def handle_message(self, msg: ActorMessage) -> ActorResult:
        intent = classify_intent(msg.content)
        prefer_quality = intent in (MessageIntent.TASK, MessageIntent.SUPERVISE)

        model = self._router.select_model(msg.content, prefer_quality=prefer_quality)
        target = route_by_content(msg.content, self._system._actors)

        if target:
            logger.info("AdaptiveRouter: %s -> model=%s, actor=%s, intent=%s",
                        msg.content[:40], model, target, intent.value)
            actor_inst = self._system.get(target)
            if actor_inst:
                result = await actor_inst.ask(msg.content, msg_type=msg.msg_type,
                                               timeout=msg.metadata.get("timeout", 120.0))
                self._router.record_result(
                    model=model,
                    success=result.success,
                    quality_score=0.8 if result.success else 0.2,
                    latency_ms=result.duration_s * 1000,
                )
                return result

        return ActorResult(success=False, content="", error=f"No route for: {msg.content[:60]}")
