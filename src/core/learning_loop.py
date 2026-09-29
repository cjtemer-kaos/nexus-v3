"""
Learning Loop — NEXUS siempre evoluciona.

Cuando el Director no sabe como hacer algo:
1. Detecta el gap de conocimiento
2. Elige estrategia de busqueda (web, repos, skills, brain)
3. Busca y analiza
4. Absorbe el nuevo conocimiento
5. Registra en memoria permanente
6. NEXUS CRECIO

"NEXUS no dice 'no puedo'. Si no sabe, aprende."
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


class LearningStrategy(str, Enum):
    SEARCH_BRAIN = "brain"          # cerebro.db — ya lo sabiamos?
    SEARCH_SKILLS = "skills"        # 1,637 skills — hay skill para esto?
    SEARCH_REPOS = "repos"          # autopsia/ — algun repo lo tiene?
    SEARCH_WEB = "web"              # web — buscar documentacion
    SEARCH_RAG = "rag"              # RAG engine — chunks indexados
    ASK_AGENT = "ask_agent"         # delegar a agente externo que sepa


@dataclass
class LearningResult:
    query: str
    strategy: LearningStrategy
    found: bool
    knowledge: str = ""
    source: str = ""
    new_capability: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "strategy": self.strategy.value,
            "found": self.found,
            "knowledge": self.knowledge[:500],
            "source": self.source,
            "new_capability": self.new_capability,
            "timestamp": self.timestamp,
        }


STRATEGY_ORDER = [
    LearningStrategy.SEARCH_BRAIN,
    LearningStrategy.SEARCH_RAG,
    LearningStrategy.SEARCH_SKILLS,
    LearningStrategy.SEARCH_REPOS,
    LearningStrategy.SEARCH_WEB,
    LearningStrategy.ASK_AGENT,
]


class LearningLoop:
    """
    Loop de aprendizaje continuo.

    NEXUS detecta gaps en su conocimiento y los llena automaticamente.
    Cada cosa que aprende queda registrada para siempre.
    """

    def __init__(self):
        self._known_capabilities: set[str] = set()
        self._learned: list[LearningResult] = []
        self._searchers: dict[LearningStrategy, Callable] = {}

    def register_known(self, *capabilities: str) -> None:
        """Registra capacidades ya conocidas."""
        self._known_capabilities.update(c.lower() for c in capabilities)

    def learn_capability(self, capability: str) -> None:
        """Registra nueva capacidad aprendida."""
        self._known_capabilities.add(capability.lower())

    def has_gap(self, task: str) -> bool:
        """Detecta si hay gap de conocimiento para esta tarea."""
        task_words = set(re.findall(r'\w+', task.lower()))
        # Remove common stop words
        stop_words = {"un", "una", "el", "la", "los", "las", "de", "del", "en", "con",
                      "para", "por", "que", "como", "a", "the", "is", "to", "and", "or",
                      "implementar", "crear", "hacer", "write", "build", "implement", "create"}
        meaningful = task_words - stop_words
        if not meaningful:
            return False
        # If any meaningful word matches a known capability, no gap
        for word in meaningful:
            if word in self._known_capabilities:
                return False
            # Partial match (e.g., "websocket" matches "websocket")
            for cap in self._known_capabilities:
                if cap in word or word in cap:
                    return False
        return True

    def detect_strategy(self, task: str) -> LearningStrategy:
        """Determina la mejor estrategia de busqueda para el gap."""
        task_lower = task.lower()
        # Technical terms -> search repos/web first
        if any(kw in task_lower for kw in ["pattern", "algorithm", "protocol", "framework"]):
            return LearningStrategy.SEARCH_REPOS
        # Specific tools/libs -> web
        if any(kw in task_lower for kw in ["install", "configure", "setup", "api", "sdk"]):
            return LearningStrategy.SEARCH_WEB
        # General knowledge -> brain first
        return LearningStrategy.SEARCH_BRAIN

    def register_searcher(self, strategy: LearningStrategy,
                          searcher: Callable[[str], Awaitable[LearningResult | None]]) -> None:
        """Registra una funcion de busqueda para una estrategia."""
        self._searchers[strategy] = searcher

    async def learn(self, task: str) -> LearningResult:
        """
        Ejecuta el Learning Loop completo:
        1. Detecta gap
        2. Prueba cada estrategia en orden
        3. Cuando encuentra, registra y retorna

        Si no encuentra nada, retorna result con found=False.
        """
        if not self.has_gap(task):
            return LearningResult(
                query=task,
                strategy=LearningStrategy.SEARCH_BRAIN,
                found=True,
                knowledge="Already known capability",
                source="internal",
            )

        # Try each strategy in order
        for strategy in STRATEGY_ORDER:
            searcher = self._searchers.get(strategy)
            if not searcher:
                continue
            try:
                result = await searcher(task)
                if result and result.found:
                    if result.new_capability:
                        self.learn_capability(result.new_capability)
                    self._learned.append(result)
                    logger.info(f"LearningLoop: learned '{result.new_capability}' from {strategy.value}")
                    return result
            except Exception as e:
                logger.warning(f"LearningLoop: {strategy.value} search failed: {e}")
                continue

        # Nothing found
        result = LearningResult(
            query=task,
            strategy=LearningStrategy.SEARCH_WEB,
            found=False,
            knowledge="No knowledge found for this task",
        )
        self._learned.append(result)
        return result

    def status(self) -> dict:
        return {
            "known_capabilities": len(self._known_capabilities),
            "total_learned": len(self._learned),
            "recent_learned": [r.to_dict() for r in self._learned[-5:]],
            "registered_searchers": [s.value for s in self._searchers.keys()],
        }
