"""Recordatorio anti-bucle por herramienta: el aviso tiene que ensenar la salida.

El bucle real observado en un turno en vivo no fue "la misma llamada repetida"
sino `job_read(cursor=0)`, `job_read(cursor=100)`, `job_read(cursor=200)`...:
argumentos distintos, misma herramienta, y el modelo pedia tramos minimos sin
usar el `next_cursor` que la propia tool devolvia. El aviso generico ("usa otra
herramienta") no le dice nada util a un polling. Para `job_read`/`job_wait` el
recordatorio explica el cursor.
"""
from __future__ import annotations

import asyncio

from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse, ToolCall
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry


class PollingProvider(BaseProvider):
    """Pide la misma herramienta con argumentos distintos (como hace un polling)."""

    def __init__(self, tool: str = "job_read"):
        self.tool = tool
        self.vistos: list[list[str]] = []

    async def chat(self, messages, tools=None, model=None, **kw):
        self.vistos.append([str(m.content or "") for m in messages])
        n = len(self.vistos)
        args = ({"job_id": "j1", "cursor": n * 100, "max_chars": 10}
                if self.tool == "job_read" else {"text": f"paso {n}"})
        return LLMResponse(content="", tool_calls=(
            ToolCall(id=f"c{n}", name=self.tool, arguments=args),),
            finish_reason="tool_calls", model="m")

    async def health(self):
        return True

    async def list_models(self):
        return ["m"]


def _correr(provider, tool_names):
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="anti-loop-hint")
            reg = ToolRegistry()
            for nombre in tool_names:
                if nombre == "job_read":
                    reg.register_function(
                        lambda job_id="", cursor=0, max_chars=0: '{"output": "", "cursor": 1}',
                        name="job_read")
                else:
                    reg.register_function(lambda text="": "ok", name=nombre)
            router = ProviderRouter()
            router.register_provider("mock", provider)
            router.register_model(ModelRoute(id="mock-1", provider_name="mock"))
            loop = AgentLoop(
                provider_router=router, tool_registry=reg, session_log=slog,
                budget=IterationBudget(max_iterations=4),
                config={"agent_loop": {"anti_loop": {
                    "repeat_stop": 99, "tool_warn": 2, "tool_stop": 99}}},
            )
            return await loop.run(task="leer el job", session_id=sess.id, hint="coding")
        finally:
            slog.close()

    return asyncio.run(run())


def test_el_recordatorio_de_polling_explica_el_cursor():
    provider = PollingProvider("job_read")
    _correr(provider, ["job_read"])

    assert len(provider.vistos) >= 3, "hacen falta 2 llamadas + la que ve el aviso"
    ultimo = "\n".join(provider.vistos[2])
    assert "next_cursor" in ultimo
    assert "max_chars" in ultimo
    assert "200000" in ultimo
    assert "job_wait" in ultimo


def test_el_recordatorio_generico_no_habla_de_cursor():
    provider = PollingProvider("echo")
    _correr(provider, ["echo"])

    ultimo = "\n".join(provider.vistos[2])
    assert "code_search" in ultimo
    assert "next_cursor" not in ultimo
