"""Una sola compactación por turno.

Caso real (sesión `hola`, 2026-10-28): el turno de 3 minutos compactó 4 veces.
Como cada resumen se re-resumía, el pedido ("investigá rea y guardalo en el
grafo") se degradó a "pendiente: confirmar si AGENTS.md existe" y el agente
terminó en un bucle de `exec`. Compactar en cascada dentro del mismo turno no
recupera contexto: lo destruye.
"""
from __future__ import annotations

import asyncio

from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse, ToolCall
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry


class ProviderConTools(BaseProvider):
    def __init__(self):
        self.n = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        if tools is None:
            return LLMResponse(content="listo", model="m")
        self.n += 1
        # Salidas distintas: el corte por resultado repetido no debe intervenir.
        return LLMResponse(
            content="", tool_calls=(
                ToolCall(id=f"c{self.n}", name="echo", arguments={"text": f"v{self.n}"}),),
            finish_reason="tool_calls", model="m")

    async def health(self):
        return True

    async def list_models(self):
        return ["m"]


class PipelineQueSiempreQuiere:
    handoff_prefix = ""

    def __init__(self):
        self.llamadas = 0

    def estimate_tokens_for_messages(self, msgs):
        return 10 ** 9

    def maybe_compact(self, msgs, max_tokens, threshold=0.75):
        return True

    def prune_for_llm(self, msgs):
        return msgs

    async def summarize_with_llm(self, msgs, limit, router, previous_summary=None,
                                 progress_messages=0, session_id=""):
        self.llamadas += 1
        return msgs, None


def test_compacta_una_sola_vez_por_turno():
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="compaction-unica")
            reg = ToolRegistry()
            reg.register_function(lambda text="": f"eco {text}", name="echo")
            router = ProviderRouter()
            router.register_provider("mock", ProviderConTools())
            router.register_model(ModelRoute(id="mock-1", provider_name="mock"))
            pipeline = PipelineQueSiempreQuiere()
            loop = AgentLoop(
                provider_router=router, tool_registry=reg, session_log=slog,
                compaction=pipeline, budget=IterationBudget(max_iterations=4),
                config={"agent_loop": {"anti_loop": {
                    "repeat_stop": 99, "tool_warn": 99, "tool_stop": 99}}},
            )
            await loop.run(task="hacé varias cosas", session_id=sess.id, hint="coding")
            return pipeline.llamadas
        finally:
            slog.close()

    assert asyncio.run(run()) == 1
