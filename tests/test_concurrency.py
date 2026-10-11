"""Concurrencia adaptativa: helpers free-thread-aware + pre-dispatch paralelo de tools."""
from __future__ import annotations

import asyncio

from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse, ToolCall
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry


class TwoReadsProvider(BaseProvider):
    """Turno 1: dos tool_calls de solo-lectura; turno 2: final."""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        if self.calls == 1:
            tcs = (
                ToolCall(id="c1", name="read", arguments={"path": "a.txt"}),
                ToolCall(id="c2", name="read", arguments={"path": "b.txt"}),
            )
            return LLMResponse(content="", tool_calls=tcs, finish_reason="tool_calls", model="m")
        return LLMResponse(content="listo", model="m")

    async def health(self):
        return True

    async def list_models(self):
        return ["m"]


def test_concurrency_info_and_map():
    from src.core.concurrency import info, map_cpu
    i = info()
    assert i["cpu"] >= 1 and i["default_workers"] >= 2
    assert map_cpu(lambda x: x * 2, [1, 2, 3]) == [2, 4, 6]
    assert map_cpu(lambda x: x, []) == []


def test_parallel_readonly_tools_preserve_order():
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="conc")
            seen: list[str] = []
            reg = ToolRegistry()
            reg.register_function(
                lambda path: (seen.append(path), f"contenido de {path}")[1],
                name="read", description="read",
            )
            router = ProviderRouter()
            router.register_provider("mock", TwoReadsProvider())
            router.register_model(ModelRoute(id="m", provider_name="mock"))
            loop = AgentLoop(provider_router=router, tool_registry=reg,
                             budget=IterationBudget(max_iterations=5), session_log=slog)
            res = await loop.run(task="lee dos", session_id=sess.id, hint="coding")
            assert res.success
            assert set(seen) == {"a.txt", "b.txt"}
            outs = [m.content for m in slog.get_messages(sess.id) if m.role == "tool"]
            assert outs and "contenido de a.txt" in outs[0]
            assert "contenido de b.txt" in outs[1]
        finally:
            slog.close()
    asyncio.run(run())


class RecordingProvider(BaseProvider):
    """Registra el 'model' con el que el router lo llama en cada turno."""

    def __init__(self):
        self.models: list[str] = []
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        self.models.append(model or "")
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", tool_calls=(ToolCall(id="c1", name="read", arguments={}),),
                               finish_reason="tool_calls", model=model or "m")
        return LLMResponse(content="ok", model=model or "m")

    async def health(self):
        return True

    async def list_models(self):
        return ["m"]


def test_sticky_model_single_per_run():
    """P0: el loop fija el modelo UNA vez por run (preserva el prompt cache)."""
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="sticky")
            reg = ToolRegistry()
            reg.register_function(lambda **k: "x", name="read", description="read")
            prov = RecordingProvider()
            router = ProviderRouter()
            router.register_provider("mock", prov)
            router.register_model(ModelRoute(id="m", provider_name="mock"))
            loop = AgentLoop(provider_router=router, tool_registry=reg,
                             budget=IterationBudget(max_iterations=5), session_log=slog)
            res = await loop.run(task="x", session_id=sess.id, hint="coding")
            assert res.success
            assert prov.models and prov.models[0] == "m", prov.models
            assert all(x == prov.models[0] for x in prov.models), prov.models
        finally:
            slog.close()
    asyncio.run(run())
