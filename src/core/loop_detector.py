"""
F16: Loop Detection - Mejorada con N-gram Jaccard Similarity

Detecta y termina agentes atrapados en patrones repetitivos.
Combina deteccion de secuencias de acciones con similaridad semantica
de respuestas y fingerprinting de llamadas a herramientas.

Caracteristicas:
- N-gram Jaccard Similarity para detectar respuestas semanticamente similares
- Tool fingerprinting para detectar "doom loops" de herramientas identicas
- Ventana deslizante configurable con umbrales adaptativos
"""

import hashlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("nexus-loop")


@dataclass
class ActionRecord:
    timestamp: float
    action: str
    params: str
    result_hash: str
    response_text: str = ""  # Texto completo de la respuesta para analisis semantico


@dataclass
class LoopPattern:
    pattern_id: str
    agent_id: str
    actions: List[str]
    count: int
    first_seen: float
    last_seen: float
    severity: str  # "warning", "critical"
    loop_type: str = "action"  # "action", "semantic", "tool_doom"


def ngram_similarity(s1: str, s2: str, n: int = 3) -> float:
    """
    Similaridad Jaccard de n-gramas.

    Retorna 0.0-1.0, donde 1.0 es coincidencia exacta.
    Rapido: O(len(s1) + len(s2)) usando operaciones de conjuntos.
    """

    def _ngrams(s: str) -> set:
        return {s[i : i + n] for i in range(len(s) - n + 1) if s.strip()}

    if not s1 or not s2:
        return 0.0

    ngrams1, ngrams2 = _ngrams(s1.lower()), _ngrams(s2.lower())
    if not ngrams1 or not ngrams2:
        return 0.0

    intersection = len(ngrams1 & ngrams2)
    union = len(ngrams1 | ngrams2)
    return intersection / union if union else 0.0


def is_stalled(
    recent_responses: List[str],
    threshold: int = 3,
    similarity_threshold: float = 0.7,
) -> bool:
    """
    Detecta stall usando similaridad n-gram.

    Detecta cuando TODAS las N respuestas consecutivas son mutuamente similares
    (>= threshold). Una sola respuesta disimilar resetea la senal.
    Esto captura frases como "Sigo atascado" vs "Estoy atascado" sin
    falsos positivos en "intento 1" vs "intento 2".
    """
    if len(recent_responses) < threshold:
        return False
    if not recent_responses[0]:
        return False

    # Cada par consecutivo debe ser similar
    for i in range(1, len(recent_responses)):
        if ngram_similarity(recent_responses[i], recent_responses[i - 1]) < similarity_threshold:
            return False
    return True


def fingerprint_tool_calls(tool_results: List[Dict]) -> List[Tuple[str, str]]:
    """
    Crea fingerprints deterministicos para llamadas a herramientas de un turno.

    Cada fingerprint es (tool_name, canonical_args_json). Sensible al orden
    asi que [search("a"), fetch("b")] != [fetch("b"), search("a")].
    """
    import json

    fingerprints = []
    for tr in tool_results:
        name = tr.get("tool_name", tr.get("tool", ""))
        args = tr.get("tool_input", tr.get("params", {}))
        try:
            canonical = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            canonical = str(args)
        fingerprints.append((name, canonical))
    return fingerprints


def is_tool_doom_loop(
    recent_tool_fingerprints: List[List[Tuple[str, str]]],
    threshold: int = 3,
    enabled: bool = True,
) -> Tuple[bool, str]:
    """
    Detecta doom loop via coincidencia exacta de fingerprints.

    Detecta cuando N turnos consecutivos invocan las mismas herramientas
    con argumentos identicos (canonicalizados). Argumentos diferentes
    significan trabajo diferente, asi que solo coinciden exactas.

    Retorna (is_doom_loop, description).
    """
    if not enabled:
        return False, ""
    if len(recent_tool_fingerprints) < threshold:
        return False, ""
    first = recent_tool_fingerprints[0]
    if not first:
        return False, ""

    # Todos los turnos en la ventana deben coincidir exactamente con el primero
    if all(fp == first for fp in recent_tool_fingerprints[1:]):
        tool_names = [name for name, _ in first]
        desc = (
            f"Doom loop: {len(recent_tool_fingerprints)} llamadas consecutivas "
            f"identicas ({', '.join(tool_names)})"
        )
        return True, desc
    return False, ""


class LoopDetector:
    """Detecta patrones repetitivos en ejecucion de agentes"""

    def __init__(
        self,
        window_size: int = 10,
        repetition_threshold: int = 3,
        min_pattern_length: int = 2,
        semantic_similarity_threshold: float = 0.7,
        stall_window: int = 4,
        tool_doom_threshold: int = 3,
    ):
        self.window_size = window_size
        self.repetition_threshold = repetition_threshold
        self.min_pattern_length = min_pattern_length
        self.semantic_similarity_threshold = semantic_similarity_threshold
        self.stall_window = stall_window
        self.tool_doom_threshold = tool_doom_threshold

        self._history: Dict[str, List[ActionRecord]] = defaultdict(list)
        self._detected_loops: List[LoopPattern] = []
        self._max_detected_loops = 200  # Cap to prevent memory leak
        self._terminated: Dict[str, int] = defaultdict(int)

        # Para deteccion semantica
        self._recent_responses: Dict[str, List[str]] = defaultdict(list)
        self._max_responses_per_agent = 50

        # Para deteccion de tool doom loops
        self._recent_tool_fingerprints: Dict[str, List[List[Tuple[str, str]]]] = defaultdict(list)
        self._max_fingerprints_per_agent = 20

    def record_action(self, agent_id: str, action: str, params: str = "", result: str = "", response_text: str = ""):
        """Registra una accion para deteccion de loops"""
        result_hash = hashlib.md5(result[:100].encode()).hexdigest()[:8]

        record = ActionRecord(
            timestamp=time.time(),
            action=action,
            params=params[:100],
            result_hash=result_hash,
            response_text=response_text[:500] if response_text else result[:500],
        )

        history = self._history[agent_id]
        history.append(record)

        # Mantener solo historial reciente
        if len(history) > self.window_size * 5:
            self._history[agent_id] = history[-self.window_size * 3 :]

        # Verificar loops de acciones
        self._check_for_loops(agent_id)

        # Verificar stall semantico
        if response_text:
            self._recent_responses[agent_id].append(response_text)
            if len(self._recent_responses[agent_id]) > self.stall_window:
                self._recent_responses[agent_id] = self._recent_responses[agent_id][-self.stall_window :]
            self._check_semantic_stall(agent_id)

    def record_tool_calls(self, agent_id: str, tool_results: List[Dict]):
        """Registra llamadas a herramientas para deteccion de doom loops"""
        fingerprints = fingerprint_tool_calls(tool_results)
        self._recent_tool_fingerprints[agent_id].append(fingerprints)
        if len(self._recent_tool_fingerprints[agent_id]) > self.tool_doom_threshold + 2:
            self._recent_tool_fingerprints[agent_id] = self._recent_tool_fingerprints[agent_id][
                -(self.tool_doom_threshold + 2) :
            ]
        self._check_tool_doom_loop(agent_id)

    def _check_for_loops(self, agent_id: str):
        """Verifica loops de secuencias de acciones"""
        history = self._history[agent_id]
        if len(history) < self.min_pattern_length * 2:
            return

        recent = history[-self.window_size :]
        actions = [r.action for r in recent]

        for pattern_len in range(self.min_pattern_length, min(len(actions) // 2, 5) + 1):
            pattern = actions[-pattern_len:]
            count = 0
            for i in range(len(actions) - pattern_len, -1, -pattern_len):
                if actions[i : i + pattern_len] == pattern:
                    count += 1
                else:
                    break

            if count >= self.repetition_threshold:
                self._report_loop(agent_id, pattern, count, loop_type="action")
                return

    def _check_semantic_stall(self, agent_id: str):
        """Verifica stall semantico usando N-gram Jaccard Similarity"""
        responses = self._recent_responses.get(agent_id, [])
        if len(responses) < self.stall_window:
            return

        recent = responses[-self.stall_window :]
        if is_stalled(recent, threshold=self.stall_window, similarity_threshold=self.semantic_similarity_threshold):
            self._report_loop(
                agent_id,
                ["semantic_stall"] * self.stall_window,
                self.stall_window,
                loop_type="semantic",
            )

    def _check_tool_doom_loop(self, agent_id: str):
        """Verifica doom loop de herramientas"""
        fingerprints = self._recent_tool_fingerprints.get(agent_id, [])
        if len(fingerprints) < self.tool_doom_threshold:
            return

        recent = fingerprints[-self.tool_doom_threshold :]
        is_doom, desc = is_tool_doom_loop(recent, threshold=self.tool_doom_threshold)
        if is_doom:
            self._report_loop(
                agent_id,
                ["tool_doom"] * self.tool_doom_threshold,
                self.tool_doom_threshold,
                loop_type="tool_doom",
            )

    def _report_loop(self, agent_id: str, pattern: List[str], count: int, loop_type: str = "action"):
        pattern_key = "->".join(pattern)
        pattern_id = hashlib.md5(f"{agent_id}:{pattern_key}:{loop_type}".encode()).hexdigest()[:8]

        # Verificar si ya fue reportado
        for loop in self._detected_loops:
            if loop.pattern_id == pattern_id:
                loop.count = count
                loop.last_seen = time.time()
                loop.severity = "critical" if count >= self.repetition_threshold * 2 else "warning"
                return

        loop = LoopPattern(
            pattern_id=pattern_id,
            agent_id=agent_id,
            actions=pattern,
            count=count,
            first_seen=time.time(),
            last_seen=time.time(),
            severity="warning",
            loop_type=loop_type,
        )
        self._detected_loops.append(loop)
        # Cap to prevent memory leak
        if len(self._detected_loops) > self._max_detected_loops:
            self._detected_loops = self._detected_loops[-self._max_detected_loops:]
        logger.warning(f"LOOP DETECTED [{loop_type}]: Agent {agent_id} pattern [{pattern_key}] x{count}")

    def should_terminate(self, agent_id: str) -> Tuple[bool, Optional[LoopPattern]]:
        """Verifica si el agente debe ser terminado por loop"""
        for loop in self._detected_loops:
            if loop.agent_id == agent_id and loop.count >= self.repetition_threshold * 2:
                return True, loop
        return False, None

    def get_agent_status(self, agent_id: str) -> Dict:
        history = self._history.get(agent_id, [])
        loops = [loop for loop in self._detected_loops if loop.agent_id == agent_id]
        return {
            "actions_recorded": len(history),
            "recent_actions": [r.action for r in history[-10:]],
            "loops_detected": len(loops),
            "terminated_count": self._terminated.get(agent_id, 0),
            "stall_responses": len(self._recent_responses.get(agent_id, [])),
            "tool_fingerprint_windows": len(self._recent_tool_fingerprints.get(agent_id, [])),
        }

    def reset_agent(self, agent_id: str):
        self._history.pop(agent_id, None)
        self._terminated.pop(agent_id, None)
        self._recent_responses.pop(agent_id, None)
        self._recent_tool_fingerprints.pop(agent_id, None)

    def get_detected_loops(self) -> List[LoopPattern]:
        return list(self._detected_loops)

    def reset(self):
        self._history.clear()
        self._detected_loops.clear()
        self._terminated.clear()
        self._recent_responses.clear()
        self._recent_tool_fingerprints.clear()

    def get_stats(self) -> Dict:
        return {
            "agents_tracked": len(self._history),
            "total_loops_detected": len(self._detected_loops),
            "total_terminations": sum(self._terminated.values()),
            "window_size": self.window_size,
            "repetition_threshold": self.repetition_threshold,
            "semantic_stall_enabled": True,
            "tool_doom_detection_enabled": True,
        }
