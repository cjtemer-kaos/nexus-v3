"""
Mixture of Agents - Agregador de inferencia paralela para SuperNEXUS v2

Ejecuta la misma tarea en multiples modelos/gemas y agrega los resultados
mediante debate y consenso. Mejora la calidad de respuestas complejas.

Patrones:
- Parallel execution en multiples modelos
- Voting/consensus para respuestas
- Debate entre agentes para decisiones complejas
- Weighted scoring por confiabilidad del modelo

Inspirado en: Mixture-of-Agents (MOA) paper
"""

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

logger = logging.getLogger("nexus-moa")


@dataclass
class AgentResponse:
    """Respuesta de un agente individual"""
    agent_id: str
    model: str
    content: str
    confidence: float = 1.0
    tokens_used: int = 0
    duration_ms: float = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AggregatedResult:
    """Resultado agregado del Mixture of Agents"""
    final_content: str
    method: str = ""  # "voting", "debate", "weighted", "best"
    agents_used: int = 0
    consensus_score: float = 0.0
    individual_responses: List[AgentResponse] = field(default_factory=list)
    duration_ms: float = 0


class MixtureOfAgents:
    """
    Agregador de inferencia paralela.

    Uso:
        moa = MixtureOfAgents(executor=director.ai_tools.execute)
        result = await moa.run(
            task="Disena la arquitectura de un sistema distribuido",
            agents=["code", "architect", "opencode"],
            method="debate",
        )
    """

    def __init__(self, executor: Callable = None):
        self.executor = executor
        self._stats = {
            "total_runs": 0,
            "total_agents_invoked": 0,
            "avg_consensus_score": 0.0,
            "methods_used": Counter(),
        }

    async def run(
        self,
        task: str,
        agents: List[str],
        method: str = "weighted",
        context: str = "",
        rounds: int = 2,  # Para debate
    ) -> AggregatedResult:
        """
        Ejecuta tarea en multiples agentes y agrega resultados.

        Metodos:
        - "voting": Cada agente vota, mayoria gana
        - "debate": Agentes debaten durante N rounds
        - "weighted": Ponderacion por confiabilidad del modelo
        - "best": Selecciona la mejor respuesta individual
        """
        start = time.time()
        self._stats["total_runs"] += 1
        self._stats["methods_used"][method] += 1

        if not self.executor:
            return AggregatedResult(
                final_content="Error: No executor configured",
                method=method,
                agents_used=0,
            )

        # Ejecutar en paralelo
        responses = await self._execute_parallel(task, agents, context)
        self._stats["total_agents_invoked"] += len(responses)

        # Agregar segun metodo
        if method == "voting":
            result = await self._aggregate_voting(responses, task)
        elif method == "debate":
            result = await self._aggregate_debate(responses, task, rounds)
        elif method == "weighted":
            result = await self._aggregate_weighted(responses)
        elif method == "best":
            result = self._select_best(responses)
        else:
            result = self._select_best(responses)

        result.duration_ms = (time.time() - start) * 1000
        result.agents_used = len(responses)

        # Actualizar estadisticas
        if self._stats["avg_consensus_score"] == 0:
            self._stats["avg_consensus_score"] = result.consensus_score
        else:
            self._stats["avg_consensus_score"] = (
                self._stats["avg_consensus_score"] * 0.9 + result.consensus_score * 0.1
            )

        logger.info(
            f"MOA completed: method={method}, agents={len(responses)}, "
            f"consensus={result.consensus_score:.2f}"
        )

        return result

    async def _execute_parallel(self, task: str, agents: List[str], context: str) -> List[AgentResponse]:
        """Ejecuta tarea en multiples agentes en paralelo"""
        async def run_agent(agent_id: str):
            try:
                if asyncio.iscoroutinefunction(self.executor):
                    result = await self.executor(task, gem=agent_id, context=context)
                else:
                    result = self.executor(task, gem=agent_id, context=context)

                if isinstance(result, dict):
                    return AgentResponse(
                        agent_id=agent_id,
                        model=result.get("model", agent_id),
                        content=result.get("content", ""),
                        confidence=result.get("confidence", 1.0),
                        tokens_used=result.get("tokens_used", 0),
                        duration_ms=result.get("duration_ms", 0),
                    )
                else:
                    return AgentResponse(
                        agent_id=agent_id,
                        model=agent_id,
                        content=str(result),
                    )

            except Exception as e:
                logger.warning(f"Agent {agent_id} failed: {e}")
                return AgentResponse(
                    agent_id=agent_id,
                    model=agent_id,
                    content="",
                    confidence=0.0,
                    metadata={"error": str(e)},
                )

        tasks = [run_agent(agent_id) for agent_id in agents]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        return [
            r for r in responses
            if isinstance(r, AgentResponse) and r.content
        ]

    async def _aggregate_voting(self, responses: List[AgentResponse], task: str) -> AggregatedResult:
        """
        Agregacion por votacion.

        Cada agente vota por la respuesta que considera mejor.
        La respuesta con mas votos gana.
        """
        if not responses:
            return AggregatedResult(final_content="No responses", method="voting")

        # Si solo hay una respuesta, usarla
        if len(responses) == 1:
            return AggregatedResult(
                final_content=responses[0].content,
                method="voting",
                consensus_score=1.0,
                individual_responses=responses,
            )

        # Extraer puntos clave de cada respuesta
        key_points = []
        for r in responses:
            points = self._extract_key_points(r.content)
            key_points.append(points)

        # Encontrar puntos comunes
        all_points = [p for points in key_points for p in points]
        point_counts = Counter(all_points)
        common_points = [p for p, count in point_counts.most_common() if count >= len(responses) // 2]

        # Construir respuesta final con puntos consensuados
        if common_points:
            final = f"Consenso de {len(responses)} agentes:\n\n"
            for i, point in enumerate(common_points, 1):
                final += f"{i}. {point}\n"
        else:
            # Si no hay consenso, usar la respuesta con mayor confianza
            best = max(responses, key=lambda r: r.confidence)
            final = best.content

        consensus = len(common_points) / max(1, len(set(all_points)))

        return AggregatedResult(
            final_content=final,
            method="voting",
            consensus_score=min(1.0, consensus),
            individual_responses=responses,
        )

    async def _aggregate_debate(self, responses: List[AgentResponse], task: str, rounds: int) -> AggregatedResult:
        """
        Agregacion por debate.

        Los agentes debaten durante N rounds, refinando la respuesta.
        """
        if not responses:
            return AggregatedResult(final_content="No responses", method="debate")

        if len(responses) == 1:
            return AggregatedResult(
                final_content=responses[0].content,
                method="debate",
                consensus_score=1.0,
                individual_responses=responses,
            )

        # Round 1: Cada agente presenta su posicion
        debate_history = []
        for r in responses:
            debate_history.append(f"[{r.agent_id}]: {r.content[:200]}...")

        # Rounds de debate (simulado - en produccion usaria LLM)
        for round_num in range(1, rounds):
            # Sintetizar puntos de acuerdo y desacuerdo
            synthesis = self._synthesize_debate(debate_history, round_num)
            debate_history.append(f"[Round {round_num} synthesis]: {synthesis}")

        # Respuesta final: sintesis del debate
        final = self._finalize_debate(debate_history, task)

        return AggregatedResult(
            final_content=final,
            method="debate",
            consensus_score=0.7,  # Debate siempre tiene cierto desacuerdo
            individual_responses=responses,
        )

    async def _aggregate_weighted(self, responses: List[AgentResponse]) -> AggregatedResult:
        """
        Agregacion ponderada.

        Combina respuestas ponderando por confiabilidad del modelo.
        """
        if not responses:
            return AggregatedResult(final_content="No responses", method="weighted")

        # Ponderar por confianza
        weights = {r.agent_id: r.confidence for r in responses}
        total_weight = sum(weights.values())

        if total_weight == 0:
            return self._select_best(responses)

        # Seleccionar respuesta con mayor peso
        best = max(responses, key=lambda r: weights[r.agent_id])

        # Calcular consenso (que tan similares son las respuestas)
        similarities = []
        for i, r1 in enumerate(responses):
            for r2 in responses[i + 1:]:
                sim = self._text_similarity(r1.content, r2.content)
                similarities.append(sim)

        consensus = sum(similarities) / len(similarities) if similarities else 0

        return AggregatedResult(
            final_content=best.content,
            method="weighted",
            consensus_score=consensus,
            individual_responses=responses,
        )

    def _select_best(self, responses: List[AgentResponse]) -> AggregatedResult:
        """Selecciona la mejor respuesta individual"""
        if not responses:
            return AggregatedResult(final_content="No responses", method="best")

        best = max(responses, key=lambda r: (r.confidence, len(r.content)))
        return AggregatedResult(
            final_content=best.content,
            method="best",
            consensus_score=best.confidence,
            individual_responses=responses,
        )

    def _extract_key_points(self, content: str) -> List[str]:
        """Extrae puntos clave de un texto"""
        points = []
        for line in content.split("\n"):
            line = line.strip()
            if line and len(line) > 20:
                # Detectar puntos con numeracion o bullets
                if any(line.startswith(p) for p in ["1.", "2.", "3.", "4.", "5.", "-", "*", "•"]):
                    points.append(line.lstrip("0123456789.-*• "))
                # Oraciones que parecen conclusiones
                elif any(kw in line.lower() for kw in ["conclusion", "resumen", "key", "important", "critical"]):
                    points.append(line)
        return points[:10]  # Max 10 puntos

    def _synthesize_debate(self, history: List[str], round_num: int) -> str:
        """Sintetiza un round de debate"""
        # En produccion, esto usaria un LLM para sintetizar
        # Aqui hacemos una sintesis basica
        agreements = []
        disagreements = []

        for entry in history:
            if "agree" in entry.lower() or "concur" in entry.lower():
                agreements.append(entry)
            elif "disagree" in entry.lower() or "however" in entry.lower():
                disagreements.append(entry)

        synthesis = f"Round {round_num}: "
        if agreements:
            synthesis += f"Puntos de acuerdo: {len(agreements)}. "
        if disagreements:
            synthesis += f"Puntos de desacuerdo: {len(disagreements)}. "
        synthesis += "Refinando respuesta final."

        return synthesis

    def _finalize_debate(self, history: List[str], task: str) -> str:
        """Finaliza el debate con respuesta consensuada"""
        return (
            f"Tras debate entre {len(history)} participantes:\n\n"
            f"Task: {task}\n\n"
            f"Conclusiones del debate:\n"
            + "\n".join(f"- {h[:100]}..." for h in history[-3:])
        )

    def _text_similarity(self, text1: str, text2: str) -> float:
        """Calcula similaridad simple entre dos textos"""
        if not text1 or not text2:
            return 0.0

        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())

        if not words1 or not words2:
            return 0.0

        intersection = words1 & words2
        union = words1 | words2

        return len(intersection) / len(union) if union else 0.0

    def get_stats(self) -> Dict:
        return {
            **self._stats,
            "methods_used": dict(self._stats["methods_used"]),
        }
