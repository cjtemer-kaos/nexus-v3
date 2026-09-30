from __future__ import annotations
"""LearningService — re-wiring del ciclo de autoaprendizaje P1 (ciclo #113) en el camino vivo.

Por que existe (colmena #132 / fact_5846): los modulos P1 estaban exportados en el
baseline (EXPORT_GAP 239/239) pero el wiring de v2 estaba roto:
  * director.py registraba actor_system ANTES de _init_learning_systems ->
    SelfLearningLoop y AdaptiveRouterActor NUNCA se registraban.
  * TASK_COMPLETE nunca se emitia (_is_complete siempre False).
  * learning_loop aprendia en memoria y lo descartaba (sin persistencia).
  * dream_distill no tenia scheduler (solo docstring 7d/30d).
  * ningun cambio de modelo/prompt/herramientas se persistia (sin gate).

Aqui el ciclo se cablea en orden explicito y SIN depender del actor system:

  Paso 1 (telemetria):  cada turno -> EventStore kind=task_completed (WAL).
  Paso 2 (judge->Thompson): QualityJudge + AdaptiveRouter.record_result (persiste
         router_state.json) + SelfLearningLoop batch (cada 120s -> SQLite
         learning_loop.db en el brain). El batch NO re-envia outcomes al router
         (adaptive_router=None) para evitar doble conteo.
  Paso 3 (insights ExpeL): patrones del batch -> LearningGraph nodo CONCEPT,
         aristas SUPERSEDES cuando un insight nuevo reemplaza al anterior de la
         misma familia.
  Paso 4 (SkillOpt): skill_opt.py — cambios de harness solo se persisten si
         validan held-out (fail-closed).
  Scheduler: dream_distill con intervalo real (default 24h), off en tests.

Todo lo que toca el camino de chat es fire-and-forget con try/except: el
aprendizaje NUNCA puede romper una respuesta.
"""
import asyncio
import json
import logging
import re
import time
from types import SimpleNamespace
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DREAM_INTERVAL_S = 86400.0  # 24h (v2 solo lo prometia en el docstring)


class LearningService:
    """Ciclo de autoaprendizaje cableado en el orden correcto."""

    def __init__(
        self,
        *,
        events: Any = None,
        router: Any = None,
        graph: Any = None,
        judge: Any = None,
        learning_loop: Any = None,
        skill_opt: Any = None,
        interval_s: float = 120.0,
    ):
        from src.core.event_store import EventStore
        from src.core.adaptive_router import AdaptiveRouter
        from src.core.quality_judge import get_judge
        from src.core.self_learning_loop import SelfLearningLoop
        from src.core.skill_opt import SkillOptimizer

        self.events = events if events is not None else EventStore()
        self.router = router if router is not None else AdaptiveRouter()
        self.judge = judge if judge is not None else get_judge()
        self.graph = graph  # lazy: get_learning_graph() en primer patron
        self.skill_opt = skill_opt if skill_opt is not None else SkillOptimizer()
        # adaptive_router=None A PROPOSITO: el outcome ya se registro en
        # record_turn(); pasarlo aqui duplicaria cada muestra de Thompson.
        self.learning_loop = (
            learning_loop if learning_loop is not None
            else SelfLearningLoop(
                judge_fn=self._judge,
                memory_store_fn=self._store_pattern,
                adaptive_router=None,
                interval_s=interval_s,
            )
        )
        self._started = False
        self._tasks: list[asyncio.Task] = []
        self._counters = {"turns": 0, "success": 0, "quality_sum": 0.0}

    # ------------------------------------------------------------------
    # wiring helpers
    # ------------------------------------------------------------------
    def _judge(self, task: str, outcome: str):
        """judge_fn del SelfLearningLoop batch (lee .action/.confidence)."""
        try:
            qs = self.judge.judge_response(task, outcome or "")
            conf = float(getattr(qs, "overall", 0.5))
            return SimpleNamespace(confidence=conf,
                                   action="ACCEPT" if conf >= 0.5 else "REJECT")
        except Exception:
            return SimpleNamespace(confidence=0.5, action="ACCEPT")

    def _store_pattern(self, key: str, pattern: str) -> None:
        """memory_store_fn del batch -> paso 3: grafo con SUPERSEDES."""
        if not pattern:
            return
        try:
            from src.core.learning_graph import NodeType, get_learning_graph
            g = self.graph if self.graph is not None else get_learning_graph()
            self.graph = g
            m = re.match(r"^\[([^\]]+)\]", pattern)
            family = f"family={m.group(1)}" if m else "family=general"
            nodes = [n for n in g.list_nodes(NodeType.CONCEPT)
                     if n.description == family]
            for n in nodes:
                if n.label == pattern:
                    g.add_experience(n.id, 10)
                    return
            new_id = g.add_node(label=pattern, node_type=NodeType.CONCEPT,
                                description=family)
            if nodes:
                prev = max(nodes, key=lambda n: n.id)
                g.add_edge(new_id, prev.id, "SUPERSEDES")
            logger.info("LearningGraph insight #%d (%s)", new_id, family)
        except Exception as e:
            logger.debug("grafo de insights omitido: %s", e)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self, *, dream_interval_s: float = _DREAM_INTERVAL_S,
                    enable_dream: bool = True) -> None:
        if self._started:
            return
        self._started = True
        # paso 2b: batch loop de SelfLearningLoop (cada interval_s)
        try:
            await self.learning_loop.on_start()
        except Exception as e:
            logger.warning("SelfLearningLoop no arranco: %s", e)
        # scheduler de dream_distill (v2: SIN scheduler)
        if enable_dream:
            self._tasks.append(asyncio.create_task(
                self._dream_loop(dream_interval_s), name="dream-distill"))

    async def stop(self) -> None:
        try:
            await self.learning_loop.on_stop()
        except Exception:
            pass
        try:
            # sin esperar el throttle de 10 outcomes del Thompson
            self.router.flush()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._tasks.clear()
        self._started = False

    async def _dream_loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.run_dream()
            except Exception as e:
                logger.warning("dream programado fallido: %s", e)

    async def run_dream(self) -> dict:
        """Un ciclo dream+distill manual o programado. Best-effort."""
        from src.core.dream_distill import DreamDistillEngine
        try:
            engine = DreamDistillEngine(get_observations_fn=None)
            cycle = await engine.dream()
            return {"status": getattr(cycle, "status", "?"),
                    "insights": getattr(cycle, "insight_count", 0)}
        except Exception as e:
            logger.warning("dream omitido: %s", e)
            return {"status": "failed", "error": str(e)}

    # ------------------------------------------------------------------
    # camino vivo: un turno del agente
    # ------------------------------------------------------------------
    async def record_turn(
        self,
        *,
        task: str,
        output: str,
        model: str = "",
        success: bool = True,
        session_id: str = "",
        tokens: int = 0,
        latency_ms: int = 0,
    ) -> dict:
        """Pipeline de aprendizaje por turno. Nunca lanza."""
        quality = 0.5
        try:
            qs = self.judge.judge_response(task, output or "")
            quality = float(getattr(qs, "overall", 0.5))
        except Exception as e:
            logger.debug("judge directo omitido: %s", e)

        model_used = model or "unknown"
        try:
            # paso 1: telemetría (EventStore WAL, kind libre task_completed)
            self.events.append_event(
                kind="task_completed",
                data={"task": task[:300], "model": model_used,
                      "success": bool(success), "quality": round(quality, 4),
                      "tokens": int(tokens), "latency_ms": int(latency_ms)},
                session_id=session_id,
            )
        except Exception as e:
            logger.debug("telemetria omitida: %s", e)

        try:
            # paso 2a: Thompson inmediato con el outcome real
            self.router.record_result(
                model=model_used, success=bool(success),
                quality_score=quality, latency_ms=float(latency_ms))
        except Exception as e:
            logger.debug("router record omitido: %s", e)

        try:
            # paso 2b: batch del SelfLearningLoop (judge profundo + patron + grafo)
            from src.core.actor_base import ActorMessage
            await self.learning_loop.handle_message(ActorMessage(
                msg_type="learn",
                content=json.dumps({
                    "task": task[:500],
                    "model": model_used,
                    "outcome": (output or "")[:500],
                    "quality": quality,
                    "metadata": {"success": bool(success), "session_id": session_id},
                }),
            ))
        except Exception as e:
            logger.debug("batch learn omitido: %s", e)

        self._counters["turns"] += 1
        if success:
            self._counters["success"] += 1
        self._counters["quality_sum"] += quality
        return {"quality": round(quality, 4), "model": model_used,
                "recorded": True}

    # ------------------------------------------------------------------
    # vistas
    # ------------------------------------------------------------------
    def recommend(self, task: str) -> dict:
        """Paso 2: el router Thompson recomienda modelo para una tarea."""
        try:
            model = self.router.select_model(task)
            return {"task": task, "model": model,
                    "stats": self.router.get_stats()}
        except Exception as e:
            return {"task": task, "model": None, "error": str(e)}

    def stats(self) -> dict:
        turns = self._counters["turns"]
        try:
            events_count = self.events.count(kind="task_completed")
        except Exception:
            events_count = -1
        try:
            graph_summary = {"nodes": len(self.graph.list_nodes())
                             if self.graph is not None else 0}
        except Exception:
            graph_summary = {"nodes": -1}
        return {
            "started": self._started,
            "turns": turns,
            "success": self._counters["success"],
            "avg_quality": round(self._counters["quality_sum"] / turns, 4)
            if turns else 0.0,
            "events_task_completed": events_count,
            "self_learning": self.learning_loop.get_stats(),
            "router": self.router.get_stats(),
            "graph": graph_summary,
            "skill_opt": self.skill_opt.stats(),
        }


_service: Optional[LearningService] = None


def get_learning_service() -> LearningService:
    global _service
    if _service is None:
        _service = LearningService()
    return _service


def reset_learning_service() -> None:
    """Solo para tests."""
    global _service
    _service = None
