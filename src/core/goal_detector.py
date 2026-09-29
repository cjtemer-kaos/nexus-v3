"""
Goal Complexity Detector — F12: Simple Goal Short-Circuit

Detects simple goals that can bypass the coordinator and be dispatched directly.
Uses regex patterns, keyword affinity, and length thresholds.
"""

import re
import logging
from typing import Dict
from dataclasses import dataclass

logger = logging.getLogger("nexus-goal")


@dataclass
class GoalAnalysis:
    is_simple: bool
    complexity_score: float
    reason: str
    suggested_gem: str
    bypass_coordinator: bool


SIMPLE_PATTERNS = [
    (r"^(hola|hey|buenas|saludos|que tal)", "greeting"),
    (r"^(quien eres|que eres|como te llamas|presentate)", "identity"),
    (r"^(que puedes hacer|que sabes|cuales son tus capacidades|help|ayuda)", "capabilities"),
    (r"^(estado|status|como estas|system status|health check)", "status"),
    (r"^(lista|list|muestrame|show me) (los |las )?(modelos|gems|gemas|skills|proyectos)", "list_request"),
    (r"^(responde|answer|dime|tell me) .{0,50}$", "simple_question"),
    (r"^(traduce|translate) .{0,100}$", "translation"),
    (r"^(resume|summarize|resumen) .{0,200}$", "summarize"),
    (r"^(calcula|calculate|cuanto es|what is) \d+", "math"),
    (r"^(que hora es|what time|fecha|date|dia de hoy)", "datetime"),
    (r"^(gracias|thanks|adios|bye|chao|hasta luego)", "closing"),
    (r"^(\d+\s*[\+\-\*/]\s*\d+)", "math_expression"),
    (r"^(define|que es|what is|explicame) [a-z]{0,50}$", "definition"),
]

COMPLEX_KEYWORDS = [
    "arquitectura", "architecture", "implementar", "implement", "desarrollar",
    "develop", "crear un sistema", "create a system", "pipeline", "workflow",
    "integrar", "integrate", "migrar", "migrate", "refactorizar", "refactor",
    "debuggear", "debug", "investigar", "research", "analizar", "analyze",
    "optimizar", "optimize", "desplegar", "deploy", "configurar", "configure",
    "resolver", "solve", "problem", "problema", "error", "bug",
]

GEM_MAPPING = {
    "greeting": "director",
    "identity": "director",
    "capabilities": "director",
    "status": "director",
    "list_request": "director",
    "simple_question": "director",
    "translation": "director",
    "summarize": "director",
    "math": "analyst",
    "datetime": "director",
    "closing": "director",
    "math_expression": "analyst",
    "definition": "scholar",
}


class GoalDetector:
    """Detects if a goal is simple enough to bypass coordination"""

    def __init__(self, max_simple_length: int = 150, simple_threshold: float = 0.3):
        self.max_simple_length = max_simple_length
        self.simple_threshold = simple_threshold

    def analyze(self, goal: str) -> GoalAnalysis:
        goal_lower = goal.lower().strip()

        # Length check
        if len(goal_lower) > self.max_simple_length:
            return GoalAnalysis(
                is_simple=False,
                complexity_score=1.0,
                reason=f"Too long ({len(goal_lower)} chars)",
                suggested_gem="director",
                bypass_coordinator=False,
            )

        # Pattern matching
        for pattern, category in SIMPLE_PATTERNS:
            if re.match(pattern, goal_lower):
                return GoalAnalysis(
                    is_simple=True,
                    complexity_score=0.1,
                    reason=f"Simple pattern: {category}",
                    suggested_gem=GEM_MAPPING.get(category, "director"),
                    bypass_coordinator=True,
                )

        # Keyword complexity scoring
        complex_count = sum(1 for kw in COMPLEX_KEYWORDS if kw in goal_lower)
        complex_ratio = complex_count / max(len(COMPLEX_KEYWORDS) * 0.1, 1)

        # Word count factor
        word_count = len(goal_lower.split())
        length_factor = min(word_count / 20, 1.0)

        complexity_score = (complex_ratio * 0.6) + (length_factor * 0.4)

        is_simple = complexity_score < self.simple_threshold

        return GoalAnalysis(
            is_simple=is_simple,
            complexity_score=round(complexity_score, 2),
            reason="Complex keywords" if not is_simple else "No complex patterns",
            suggested_gem="director",
            bypass_coordinator=is_simple,
        )

    def get_stats(self) -> Dict:
        return {
            "simple_patterns": len(SIMPLE_PATTERNS),
            "complex_keywords": len(COMPLEX_KEYWORDS),
            "max_simple_length": self.max_simple_length,
            "simple_threshold": self.simple_threshold,
        }
