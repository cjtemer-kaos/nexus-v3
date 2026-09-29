"""
LoopGuard - Detector de loops agenciales avanzado

Fusion de patrones de DeepSeek-TUI:
1. Canonical JSON hashing (DeepSeek-TUI) - Detecta repeticion exacta de tool calls
2. N-gram Jaccard Similarity - Detecta repeticion semantica
3. Tool fingerprinting - Detecta patrones de herramientas repetitivas

Cuando detecta un loop:
1. Congela la ejecucion
2. Notifica al Director
3. Sugiere alternativa
4. Registra en memoria para aprendizaje
"""

import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LoopDetection:
    """Resultado de deteccion de loop"""
    is_loop: bool
    loop_type: str  # "exact", "semantic", "tool_pattern"
    similarity: float  # 0.0 - 1.0
    loop_length: int  # Cuantas iteraciones se repiten
    affected_steps: List[int]  # Indices de los pasos afectados
    suggestion: str  # Sugerencia para romper el loop
    timestamp: float = field(default_factory=time.time)


class CanonicalHasher:
    """Genera hashes canonicos de tool calls para deteccion exacta"""

    @staticmethod
    def hash_tool_call(tool_name: str, params: Dict) -> str:
        """Genera hash canonico de una llamada a herramienta"""
        # Normalizar: ordenar keys, convertir a string canonico
        canonical = {
            "tool": tool_name,
            "params": CanonicalHasher._normalize(params),
        }
        json_str = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(json_str.encode()).hexdigest()[:16]

    @staticmethod
    def _normalize(obj: Any) -> Any:
        """Normaliza un objeto para hashing canonico"""
        if isinstance(obj, dict):
            return {k: CanonicalHasher._normalize(v) for k, v in sorted(obj.items())}
        if isinstance(obj, list):
            return [CanonicalHasher._normalize(item) for item in obj]
        if isinstance(obj, float):
            return round(obj, 4)  # Redondear floats para evitar diferencias minimas
        return obj


class NgramJaccard:
    """Calcula similitud Jaccard entre n-grams de texto"""

    @staticmethod
    def similarity(text1: str, text2: str, n: int = 3) -> float:
        """Calcula similitud Jaccard entre n-grams de dos textos"""
        if not text1 or not text2:
            return 0.0

        ngrams1 = NgramJaccard._get_ngrams(text1.lower(), n)
        ngrams2 = NgramJaccard._get_ngrams(text2.lower(), n)

        if not ngrams1 or not ngrams2:
            return 0.0

        intersection = ngrams1 & ngrams2
        union = ngrams1 | ngrams2

        return len(intersection) / len(union) if union else 0.0

    @staticmethod
    def _get_ngrams(text: str, n: int) -> set:
        """Extrae n-grams de un texto"""
        words = text.split()
        if len(words) < n:
            return set()
        return set(tuple(words[i:i+n]) for i in range(len(words) - n + 1))


class LoopGuard:
    """Detector de loops agenciales con multiple estrategias"""

    # Tools que NO deben considerarse como loops (interacción normal)
    EXCLUDED_TOOLS = frozenset({
        "user_input", "chat", "send_message", "read_messages",
        "human_input", "ask_user", "wait_input", "prompt",
    })

    def __init__(
        self,
        max_history: int = 50,
        exact_threshold: int = 3,  # Repeticiones exactas para detectar loop
        semantic_threshold: float = 0.85,  # Similitud Jaccard para loop semantico
        tool_pattern_threshold: int = 5,  # Repeticiones de patron de herramientas
    ):
        self.max_history = max_history
        self.exact_threshold = exact_threshold
        self.semantic_threshold = semantic_threshold
        self.tool_pattern_threshold = tool_pattern_threshold

        # Historial de tool calls
        self._tool_calls: Deque[Dict] = deque(maxlen=max_history)
        self._responses: Deque[str] = deque(maxlen=max_history)
        self._hash_history: Deque[str] = deque(maxlen=max_history)

        # Estadisticas
        self.loops_detected = 0
        self.total_checks = 0

    def check(
        self,
        tool_name: str,
        params: Dict,
        response_text: str = "",
    ) -> Optional[LoopDetection]:
        """Verifica si la nueva llamada indica un loop"""
        self.total_checks += 1

        # Skip excluded tools (user interaction, not loops)
        if tool_name.lower() in self.EXCLUDED_TOOLS:
            return None

        # Generar hash canonico
        call_hash = CanonicalHasher.hash_tool_call(tool_name, params)

        # Registrar en historial
        self._tool_calls.append({"tool": tool_name, "params": params})
        self._responses.append(response_text)
        self._hash_history.append(call_hash)

        # Verificar loop exacto
        exact_loop = self._check_exact_loop(call_hash)
        if exact_loop:
            return exact_loop

        # Verificar loop semantico
        semantic_loop = self._check_semantic_loop(response_text)
        if semantic_loop:
            return semantic_loop

        # Verificar patron de herramientas
        tool_pattern = self._check_tool_pattern()
        if tool_pattern:
            return tool_pattern

        return None

    def _check_exact_loop(self, current_hash: str) -> Optional[LoopDetection]:
        """Detecta repeticion exacta de tool calls"""
        hash_list = list(self._hash_history)
        if len(hash_list) < self.exact_threshold:
            return None

        # Verificar si los ultimos N hashes son iguales
        recent = hash_list[-self.exact_threshold:]
        if len(set(recent)) == 1:
            self.loops_detected += 1
            return LoopDetection(
                is_loop=True,
                loop_type="exact",
                similarity=1.0,
                loop_length=self.exact_threshold,
                affected_steps=list(range(len(hash_list) - self.exact_threshold, len(hash_list))),
                suggestion="La misma llamada se repite exactamente. Considera cambiar parametros o usar otra herramienta.",
            )

        return None

    def _check_semantic_loop(self, current_response: str) -> Optional[LoopDetection]:
        """Detecta repeticion semantica en respuestas"""
        if not current_response or len(self._responses) < 3:
            return None

        responses = list(self._responses)
        recent = responses[-3:]

        # Calcular similitud entre respuestas recientes
        similarities = []
        for i in range(len(recent) - 1):
            sim = NgramJaccard.similarity(recent[i], recent[i + 1], n=3)
            similarities.append(sim)

        if similarities and all(s >= self.semantic_threshold for s in similarities):
            self.loops_detected += 1
            avg_sim = sum(similarities) / len(similarities)
            return LoopDetection(
                is_loop=True,
                loop_type="semantic",
                similarity=avg_sim,
                loop_length=len(recent),
                affected_steps=list(range(len(responses) - len(recent), len(responses))),
                suggestion="Las respuestas son semanticamente similares. El agente puede estar en un bucle de razonamiento.",
            )

        return None

    def _check_tool_pattern(self) -> Optional[LoopDetection]:
        """Detecta patrones repetitivos de herramientas"""
        if len(self._tool_calls) < self.tool_pattern_threshold:
            return None

        calls = list(self._tool_calls)
        recent = calls[-self.tool_pattern_threshold:]

        # Verificar si se usa la misma herramienta repetidamente
        tool_names = [c["tool"] for c in recent]
        if len(set(tool_names)) == 1:
            self.loops_detected += 1
            return LoopDetection(
                is_loop=True,
                loop_type="tool_pattern",
                similarity=1.0,
                loop_length=self.tool_pattern_threshold,
                affected_steps=list(range(len(calls) - self.tool_pattern_threshold, len(calls))),
                suggestion=f"La herramienta '{tool_names[0]}' se usa repetidamente. Considera una estrategia diferente.",
            )

        return None

    def get_stats(self) -> Dict:
        """Estadisticas del LoopGuard"""
        return {
            "loops_detected": self.loops_detected,
            "total_checks": self.total_checks,
            "loop_rate": self.loops_detected / max(1, self.total_checks),
            "history_size": len(self._tool_calls),
        }

    def reset(self):
        """Reinicia el historial"""
        self._tool_calls.clear()
        self._responses.clear()
        self._hash_history.clear()
