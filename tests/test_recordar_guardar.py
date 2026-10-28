"""Si el usuario pide guardar en el grafo, el turno no cierra sin guardar.

Caso real (sesión `hola`, 2026-10-28): el pedido era "usá internet para aprender
más de rea ... después meté toda esa investigación en tu grafo". El turno gastó
cinco búsquedas, perdió el hilo en la compactación y respondió con el tag del
bucle: cero `learn` ejecutados, investigación perdida.
"""
from __future__ import annotations

import asyncio

from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse, ToolCall
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry


class ProviderQueOlvida(BaseProvider):
    """Responde texto sin guardar; sólo guarda si el harness se lo recuerda."""

    def __init__(self):
        self.vistos: list[list[str]] = []
        self.guardado = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        self.vistos.append([str(m.content or "") for m in messages])
        if tools is None:
            return LLMResponse(content="listo", model="m")
        recordado = any("todavía no llamaste a 'learn'" in str(m.content or "") for m in messages)
        if recordado and self.guardado < 1:
            self.guardado += 1
            return LLMResponse(
                content="", tool_calls=(ToolCall(id="c-learn", name="learn",
                                                arguments={"topic": "rea", "content": "x"}),),
                finish_reason="tool_calls", model="m")
        return LLMResponse(content="Ya investigué rea. Listo.", model="m")

    async def health(self):
        return True

    async def list_models(self):
        return ["m"]


def _correr(task: str):
    estado = {"learn": 0}

    def learn(topic: str = "", content: str = ""):
        estado["learn"] += 1
        return '{"ok": true}'

    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="recordar-guardar")
            reg = ToolRegistry()
            reg.register_function(learn, name="learn")
            provider = ProviderQueOlvida()
            router = ProviderRouter()
            router.register_provider("mock", provider)
            router.register_model(ModelRoute(id="mock-1", provider_name="mock"))
            loop = AgentLoop(
                provider_router=router, tool_registry=reg, session_log=slog,
                budget=IterationBudget(max_iterations=5),
                config={"agent_loop": {"anti_loop": {
                    "repeat_stop": 99, "tool_warn": 99, "tool_stop": 99}}},
            )
            await loop.run(task=task, session_id=sess.id, hint="coding")
            return estado["learn"], len(provider.vistos)
        finally:
            slog.close()

    return asyncio.run(run())


def test_pedido_de_guardar_en_el_grafo_termina_con_learn():
    learn, llamadas = _correr(
        "usa internet para aprender mas de rea, despues mete toda esa investigacion en tu grafo")
    assert learn == 1, "el turno cerró sin guardar lo investigado"
    assert llamadas >= 2


def test_pedido_sin_guardar_no_agrega_recordatorio():
    learn, llamadas = _correr("explicame que es el grafo de conocimiento")
    assert learn == 0
    assert llamadas == 1
