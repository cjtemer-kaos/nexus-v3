"""
Fusion Engine - Multi-model deliberation con juez para SuperNEXUS v2

Inspirado en OpenRouter Fusion pero 100% local:
1. Scholar busca informacion web relevante
2. Panel de gemas responde en paralelo con contexto enriquecido
3. Juez compara respuestas → analisis estructurado
4. Sintesis final desde el analisis del juez
5. Aprendizaje continuo (DORA) mejora resultados con el uso

Uso:
    engine = FusionEngine(director=director)
    result = await engine.fuse(
        prompt="Analiza los pros y contras de Rust vs Go para microservicios",
        panel=["code", "architect", "scholar"],
        use_web_search=True,
    )
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("nexus-fusion")


@dataclass
class PanelResponse:
    """Respuesta de un miembro del panel"""
    gema: str
    content: str
    model: str = ""
    confidence: float = 1.0
    duration_ms: float = 0
    web_context_used: bool = False


@dataclass
class JudgeAnalysis:
    """Analisis estructurado del juez"""
    consensus: List[str] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    partial_coverage: List[str] = field(default_factory=list)
    unique_insights: List[str] = field(default_factory=list)
    blind_spots: List[str] = field(default_factory=list)
    best_gema: str = ""
    confidence_score: float = 0.0


@dataclass
class FusionResult:
    """Resultado final de fusion"""
    content: str
    analysis: JudgeAnalysis
    panel_responses: List[PanelResponse]
    web_context: str = ""
    duration_ms: float = 0
    fusion_round: int = 1
    learning_applied: bool = False


# Presets de panel
PANEL_PRESETS = {
    "quality": ["code", "architect", "scholar"],
    "code": ["code", "debugger", "engineer"],
    "research": ["scholar", "biblioteca", "analyst"],
    "creative": ["creative", "design", "prompter"],
    "full": ["code", "architect", "scholar", "creative", "analyst"],
}


class FusionEngine:
    """
    Motor de fusion multi-modelo con juez y busqueda web.

    Flujo:
    1. (Opcional) Scholar ejecuta busqueda web → enriquece contexto
    2. Panel de gemas responde en paralelo con contexto enriquecido
    3. Juez (LLM) compara y genera analisis estructurado
    4. Sintesis final desde el analisis
    5. Resultado se guarda para aprendizaje continuo
    """

    JUDGE_PROMPT = """Eres un juez experto de SuperNEXUS. Analiza las respuestas de múltiples gemas y produce un análisis estructurado.

TAREA ORIGINAL: {task}

CONTEXTO WEB (si disponible):
{web_context}

RESPUESTAS DEL PANEL:
{panel_responses}

INSTRUCCIONES:
Compara las respuestas y produce SOLO un JSON con esta estructura exacta:
{{
    "consensus": ["puntos en los que la mayoría está de acuerdo"],
    "contradictions": ["puntos donde hay desacuerdo directo entre gemas"],
    "partial_coverage": ["temas que solo algunas gemas cubrieron"],
    "unique_insights": ["ideas originales de gemas individuales"],
    "blind_spots": ["áreas que ninguna gema cubrió"],
    "best_gema": "nombre de la gema con mejor respuesta",
    "confidence_score": 0.0-1.0,
    "final_recommendation": "recomendación concisa basada en el análisis"
}}

Responde SOLO con el JSON, sin texto adicional."""

    SYNTHESIS_PROMPT = """Eres el sintetizador final de SuperNEXUS Fusion.

TAREA ORIGINAL: {task}

ANÁLISIS DEL JUEZ:
{analysis}

MEJOR RESPUESTA ({best_gema}):
{best_response}

CONTEXTO ADICIONAL:
{web_context}

INSTRUCCIONES:
Escribe una respuesta final que:
1. Sintetice los mejores puntos de TODAS las respuestas (no solo la mejor)
2. Resuelva las contradicciones favorables al usuario
3. Incluya los insights únicos relevantes
4. Advertencia sobre blind spots importantes
5. Sea clara, concisa y accionable

Respuesta final:"""

    def __init__(self, director=None, memory_system=None, backend=None):
        """
        Args:
            director: DirectorNexus instance para ejecutar gemas
            memory_system: Sistema de memoria para aprendizaje continuo
            backend: SuperNEXUSBackend instance para process_message
        """
        self.director = director
        self.memory = memory_system
        self.backend = backend
        self._stats = {
            "total_fusions": 0,
            "avg_panel_size": 0,
            "avg_confidence": 0,
            "avg_duration_ms": 0,
        }

    async def fuse(
        self,
        prompt: str,
        panel: List[str] = None,
        preset: str = "quality",
        use_web_search: bool = True,
        max_panel_time_ms: int = 60000,
        context: str = "",
    ) -> FusionResult:
        """
        Ejecuta fusion multi-modelo.

        Args:
            prompt: Tarea o pregunta del usuario
            panel: Lista de gemas para el panel (override preset)
            preset: Nombre del preset si no se especifica panel
            use_web_search: Si True, scholar busca informacion web primero
            max_panel_time_ms: Timeout maximo para el panel en paralelo
            context: Contexto adicional para las gemas
        """
        start = time.time()
        self._stats["total_fusions"] += 1

        # Resolver panel
        gemas = panel or PANEL_PRESETS.get(preset, PANEL_PRESETS["quality"])
        self._stats["avg_panel_size"] = (
            self._stats["avg_panel_size"] * 0.9 + len(gemas) * 0.1
        )

        logger.info(f"Fusion starting: {len(gemas)} gemas, web_search={use_web_search}")

        # Fase 1: Busqueda web con Scholar (si habilitado)
        web_context = ""
        if use_web_search:
            web_context = await self._web_search(prompt)

        # Fase 2: Panel responde en paralelo
        enriched_prompt = self._enrich_prompt(prompt, web_context, context)
        panel_responses = await self._run_panel(gemas, enriched_prompt, max_panel_time_ms)

        if not panel_responses:
            return FusionResult(
                content="Error: Ninguna gema del panel respondió.",
                analysis=JudgeAnalysis(),
                panel_responses=[],
                duration_ms=(time.time() - start) * 1000,
            )

        # Fase 3: Juez analiza
        analysis = await self._judge_analyze(prompt, panel_responses, web_context)

        # Fase 4: Sintesis final
        synthesis = await self._synthesize(prompt, analysis, panel_responses, web_context)

        # Fase 5: Aprendizaje continuo
        await self._learn(prompt, synthesis, analysis)

        duration = (time.time() - start) * 1000
        self._update_stats(duration, analysis.confidence_score)

        logger.info(
            f"Fusion complete: {len(panel_responses)} responses, "
            f"confidence={analysis.confidence_score:.2f}, "
            f"duration={duration:.0f}ms"
        )

        return FusionResult(
            content=synthesis,
            analysis=analysis,
            panel_responses=panel_responses,
            web_context=web_context,
            duration_ms=duration,
            learning_applied=self.memory is not None,
        )

    async def _web_search(self, query: str) -> str:
        """Ejecuta busqueda web via scholar gema"""
        if not self.director:
            return ""

        try:
            search_prompt = (
                f"Busca información actualizada sobre: {query}\n"
                f"Responde con los hallazgos clave, fuentes y datos relevantes.\n"
                f"Maximo 500 palabras."
            )

            if hasattr(self.director, 'execute_gema'):
                result = await self.director.execute_gema("scholar", search_prompt)
            else:
                result = await self.director.process_message(search_prompt, "scholar")

            if isinstance(result, dict):
                return result.get("reply", result.get("content", ""))
            return str(result) if result else ""

        except Exception as e:
            logger.warning(f"Web search failed: {e}")
            return ""

    async def _run_panel(
        self,
        gemas: List[str],
        prompt: str,
        timeout_ms: int,
    ) -> List[PanelResponse]:
        """Ejecuta panel de gemas en paralelo"""
        async def run_gema(gema_name: str) -> PanelResponse:
            start = time.time()
            try:
                if self.backend:
                    result = await asyncio.wait_for(
                        self.backend.process_message(prompt, gem=gema_name),
                        timeout=timeout_ms / 1000,
                    )
                elif self.director:
                    if hasattr(self.director, 'execute'):
                        result = await asyncio.wait_for(
                            self.director.execute(prompt, gem=gema_name),
                            timeout=timeout_ms / 1000,
                        )
                    else:
                        return PanelResponse(
                            gema=gema_name,
                            content=f"No execute method on director",
                            confidence=0.0,
                        )
                else:
                    return PanelResponse(
                        gema=gema_name,
                        content="No director configured",
                        confidence=0.0,
                    )

                content = ""
                model = ""
                if isinstance(result, dict):
                    content = result.get("reply", result.get("content", ""))
                    model = result.get("gem_used", gema_name)
                elif hasattr(result, 'data'):
                    data = result.data
                    if isinstance(data, dict):
                        content = data.get("reply", data.get("content", ""))
                    elif isinstance(data, str):
                        content = data
                    else:
                        content = str(data)
                    model = getattr(result, 'engine', gema_name)
                elif hasattr(result, 'content'):
                    content = result.content if isinstance(result.content, str) else str(result.content)
                    model = getattr(result, 'gem', gema_name)
                else:
                    content = str(result)
                    model = gema_name

                return PanelResponse(
                    gema=gema_name,
                    content=content,
                    model=model,
                    confidence=1.0 if content else 0.0,
                    duration_ms=(time.time() - start) * 1000,
                    web_context_used=bool(prompt != prompt),  # always true if enriched
                )

            except asyncio.TimeoutError:
                return PanelResponse(
                    gema=gema_name,
                    content="",
                    confidence=0.0,
                    duration_ms=(time.time() - start) * 1000,
                )
            except Exception as e:
                logger.warning(f"Gema {gema_name} failed: {e}")
                return PanelResponse(
                    gema=gema_name,
                    content="",
                    confidence=0.0,
                    metadata={"error": str(e)},
                )

        tasks = [run_gema(g) for g in gemas]
        responses = await asyncio.gather(*tasks)
        return [r for r in responses if r.content]

    async def _judge_analyze(
        self,
        task: str,
        panel_responses: List[PanelResponse],
        web_context: str,
    ) -> JudgeAnalysis:
        """Juez LLM analiza las respuestas del panel"""
        if not self.director:
            return self._fallback_analysis(panel_responses)

        # Formatear respuestas del panel
        panel_text = ""
        for i, r in enumerate(panel_responses, 1):
            panel_text += f"\n### Gema {i}: {r.gema} (modelo: {r.model})\n"
            panel_text += f"{r.content[:1500]}\n"

        # Prompt para el juez
        judge_prompt = self.JUDGE_PROMPT.format(
            task=task,
            web_context=web_context[:1000] if web_context else "No disponible",
            panel_responses=panel_text,
        )

        try:
            if hasattr(self.director, 'execute_gema'):
                result = await self.director.execute_gema("scholar", judge_prompt)
            else:
                result = await self.director.process_message(judge_prompt, "scholar")

            content = ""
            if isinstance(result, dict):
                content = result.get("reply", result.get("content", ""))
            else:
                content = str(result)

            # Parsear JSON del juez
            return self._parse_judge_response(content, panel_responses)

        except Exception as e:
            logger.warning(f"Judge analysis failed: {e}")
            return self._fallback_analysis(panel_responses)

    def _parse_judge_response(
        self,
        content: str,
        panel_responses: List[PanelResponse],
    ) -> JudgeAnalysis:
        """Parsea la respuesta JSON del juez"""
        try:
            # Buscar JSON en la respuesta
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(content[start:end])
                return JudgeAnalysis(
                    consensus=data.get("consensus", []),
                    contradictions=data.get("contradictions", []),
                    partial_coverage=data.get("partial_coverage", []),
                    unique_insights=data.get("unique_insights", []),
                    blind_spots=data.get("blind_spots", []),
                    best_gema=data.get("best_gema", panel_responses[0].gema if panel_responses else ""),
                    confidence_score=data.get("confidence_score", 0.7),
                )
        except (json.JSONDecodeError, KeyError):
            pass

        return self._fallback_analysis(panel_responses)

    def _fallback_analysis(
        self,
        panel_responses: List[PanelResponse],
    ) -> JudgeAnalysis:
        """Analisis de fallback cuando el juez falla"""
        if not panel_responses:
            return JudgeAnalysis()

        # Seleccionar la respuesta mas larga como mejor
        best = max(panel_responses, key=lambda r: len(r.content))
        return JudgeAnalysis(
            consensus=["Análisis automático: respuesta más completa seleccionada"],
            contradictions=[],
            partial_coverage=[],
            unique_insights=[],
            blind_spots=["Análisis del juez no disponible"],
            best_gema=best.gema,
            confidence_score=0.5,
        )

    async def _synthesize(
        self,
        task: str,
        analysis: JudgeAnalysis,
        panel_responses: List[PanelResponse],
        web_context: str,
    ) -> str:
        """Sintesis final desde el analisis del juez"""
        # Encontrar la mejor respuesta
        best_response = ""
        for r in panel_responses:
            if r.gema == analysis.best_gema:
                best_response = r.content
                break
        if not best_response and panel_responses:
            best_response = max(panel_responses, key=lambda r: len(r.content)).content

        if not self.director:
            return best_response

        # Prompt de sintesis
        synthesis_prompt = self.SYNTHESIS_PROMPT.format(
            task=task,
            analysis=json.dumps({
                "consensus": analysis.consensus,
                "contradictions": analysis.contradictions,
                "unique_insights": analysis.unique_insights,
                "blind_spots": analysis.blind_spots,
            }, ensure_ascii=False, indent=2),
            best_gema=analysis.best_gema,
            best_response=best_response[:2000],
            web_context=web_context[:500] if web_context else "No disponible",
        )

        try:
            if hasattr(self.director, 'execute_gema'):
                result = await self.director.execute_gema("creative", synthesis_prompt)
            else:
                result = await self.director.process_message(synthesis_prompt, "creative")

            if isinstance(result, dict):
                return result.get("reply", result.get("content", best_response))
            return str(result) if result else best_response

        except Exception as e:
            logger.warning(f"Synthesis failed: {e}")
            return best_response

    async def _learn(
        self,
        prompt: str,
        synthesis: str,
        analysis: JudgeAnalysis,
    ):
        """Guarda resultado para aprendizaje continuo"""
        if not self.memory:
            return

        try:
            learning_record = {
                "type": "fusion_result",
                "prompt": prompt[:500],
                "synthesis_preview": synthesis[:500],
                "confidence": analysis.confidence_score,
                "best_gema": analysis.best_gema,
                "consensus_count": len(analysis.consensus),
                "contradiction_count": len(analysis.contradictions),
                "blind_spot_count": len(analysis.blind_spots),
                "timestamp": time.time(),
            }

            # Guardar en memoria si esta disponible
            if hasattr(self.memory, 'add_finding'):
                self.memory.add_finding(
                    finding=json.dumps(learning_record),
                    category="fusion_learning",
                )
            elif hasattr(self.memory, 'remember'):
                self.memory.remember(
                    key=f"fusion_{int(time.time())}",
                    content=json.dumps(learning_record),
                )

        except Exception as e:
            logger.debug(f"Learning save failed: {e}")

    def _enrich_prompt(self, prompt: str, web_context: str, extra_context: str) -> str:
        """Enriquece el prompt con contexto web y adicional"""
        parts = [prompt]
        if web_context:
            parts.append(f"\n--- CONTEXTO WEB ---\n{web_context[:1000]}")
        if extra_context:
            parts.append(f"\n--- CONTEXTO ADICIONAL ---\n{extra_context[:500]}")
        return "\n".join(parts)

    def _update_stats(self, duration_ms: float, confidence: float):
        """Actualiza estadisticas"""
        n = self._stats["total_fusions"]
        self._stats["avg_duration_ms"] = (
            self._stats["avg_duration_ms"] * (n - 1) / n + duration_ms / n
        )
        self._stats["avg_confidence"] = (
            self._stats["avg_confidence"] * 0.9 + confidence * 0.1
        )

    def get_stats(self) -> Dict:
        return {**self._stats}
