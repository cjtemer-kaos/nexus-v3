"""P1 streaming (pi): providers emiten deltas; el loop los reenvia via on_delta."""
from __future__ import annotations

import asyncio

from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry


class StreamProvider(BaseProvider):
    async def chat(self, messages, tools=None, model=None, **kw):
        return LLMResponse(content="hola mundo", model="s")

    async def stream(self, messages, tools=None, model=None, **kw):
        for w in ["hola ", "mundo"]:
            yield {"type": "text", "text": w}
        yield {"type": "final", "response": LLMResponse(content="hola mundo", model="s")}

    async def health(self):
        return True

    async def list_models(self):
        return ["s"]


def test_base_provider_stream_default():
    """El default de stream() emite el contenido completo + final (sin SSE real)."""
    async def run():
        p = StreamProvider()
        p.stream = BaseProvider.stream.__get__(p, StreamProvider)  # usa el default
        evs = [ev async for ev in p.stream([])]
        assert evs[0]["type"] == "text" and evs[0]["text"] == "hola mundo"
        assert evs[-1]["type"] == "final"
    asyncio.run(run())


def test_loop_streams_deltas_via_on_delta():
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="stream")
            deltas: list[str] = []

            async def on_delta(t: str):
                deltas.append(t)

            router = ProviderRouter()
            router.register_provider("mock", StreamProvider())
            router.register_model(ModelRoute(id="m", provider_name="mock"))
            loop = AgentLoop(provider_router=router, tool_registry=ToolRegistry(),
                             budget=IterationBudget(max_iterations=3), session_log=slog)
            res = await loop.run(task="saluda", session_id=sess.id, hint="coding", on_delta=on_delta)
            assert res.success
            assert "".join(deltas) == "hola mundo"
            assert "hola mundo" in res.final_output
        finally:
            slog.close()
    asyncio.run(run())
