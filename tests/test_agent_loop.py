"""Sección [4] Agent loop TDAO (paridad run_tests.py)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from src.core.agent_loop import AgentLoop
from src.core.error_classifier import ErrorAction, classify_error
from src.core.event_store import EventStore
from src.core.hooks_engine import HooksEngine
from src.core.iteration_budget import IterationBudget
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.provider_base import BaseProvider, LLMResponse, ToolCall
from src.core.scope import Scope, ScopeConfig
from src.core.session_log import Message, SessionLog
from src.core.tool_registry import ToolRegistry
from helpers import (
    FinalAnswerProvider,
    FailingProvider,
    LoopProvider,
    SecretProvider,
    ToolThenFinalProvider,
)

ROOT = Path(__file__).resolve().parent.parent


class OrderedBatchProvider(BaseProvider):
    """Primer turno: [write_file, read_file] en el MISMO batch (write primero)."""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", tool_calls=(
                ToolCall(id="c1", name="write_file", arguments={"path": "x.txt", "content": "hola"}),
                ToolCall(id="c2", name="read_file", arguments={"path": "x.txt"}),
            ), finish_reason="tool_calls", model="batch")
        return LLMResponse(content="ok", model="batch")

    async def health(self):
        return True

    async def list_models(self):
        return ["batch"]


def test_final_answer_no_tools():
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="loop-test")
            router = ProviderRouter()
            router.register_provider("mock", FinalAnswerProvider())
            router.register_model(ModelRoute(id="final", provider_name="mock", tier=5))
            loop = AgentLoop(
                provider_router=router,
                tool_registry=ToolRegistry(),
                budget=IterationBudget(max_iterations=5),
            )
            loop.session_log = None
            res = await loop.run(task="Hola", session_id=sess.id, hint="simple")
            assert res.success and "Respuesta final" in res.final_output
        finally:
            slog.close()

    asyncio.run(run())


def test_tool_then_final():
    async def run():
        slog2 = SessionLog(":memory:")
        try:
            sess2 = slog2.create_session(project="loop-test2")
            reg = ToolRegistry()
            reg.register_function(lambda text: f"echo:{text}", name="echo", description="echo")
            router2 = ProviderRouter()
            router2.register_provider("mock", ToolThenFinalProvider())
            router2.register_model(ModelRoute(id="tool-model", provider_name="mock", tier=3))
            loop2 = AgentLoop(
                provider_router=router2,
                tool_registry=reg,
                budget=IterationBudget(max_iterations=5),
            )
            res2 = await loop2.run(task="usa la tool echo", session_id=sess2.id, hint="coding")
            assert res2.success and "Hecho tras tool" in res2.final_output and len(res2.steps) >= 3
        finally:
            slog2.close()

    asyncio.run(run())


def test_scope_denied_handling():
    async def run():
        slog3 = SessionLog(":memory:")
        try:
            sess3 = slog3.create_session(project="loop-test3")
            reg3 = ToolRegistry()

            def forbidden(path: str):
                return "ok"

            reg3.register_function(forbidden, name="read_secret", description="blocked")
            router3 = ProviderRouter()
            router3.register_provider("mock", SecretProvider())
            router3.register_model(ModelRoute(id="m", provider_name="mock"))
            scope = Scope(
                ScopeConfig(allowed_roots=("./workspace",), deny_patterns=("**/.env",)),
                project_root=ROOT,
            )
            loop3 = AgentLoop(
                provider_router=router3,
                tool_registry=reg3,
                scope=scope,
                budget=IterationBudget(max_iterations=5),
            )
            res3 = await loop3.run(task="lee secreto", session_id=sess3.id)
            has_error_step = any(
                "ScopeDenied" in s.content
                or "scope" in s.content.lower()
                or "denegada" in s.content.lower()
                for s in res3.steps
            )
            assert has_error_step or not res3.success
        finally:
            slog3.close()

    asyncio.run(run())


def test_budget_exhausted():
    async def run():
        slog4 = SessionLog(":memory:")
        try:
            sess4 = slog4.create_session(project="loop-test4")
            reg4 = ToolRegistry()
            reg4.register_function(lambda text: "ok", name="echo")
            router4 = ProviderRouter()
            router4.register_provider("mock", LoopProvider())
            router4.register_model(ModelRoute(id="loop", provider_name="mock"))
            loop4 = AgentLoop(
                provider_router=router4,
                tool_registry=reg4,
                budget=IterationBudget(max_iterations=2),
            )
            res4 = await loop4.run(task="loop forever", session_id=sess4.id)
            assert res4.iterations == 2
        finally:
            slog4.close()

    asyncio.run(run())


def test_integration_trajectory_event_store_hooks():
    async def run():
        slog5 = SessionLog(":memory:")
        try:
            sess5 = slog5.create_session(project="loop-test5")
            es = EventStore(":memory:")
            hooks = HooksEngine()
            hook_calls = []
            hooks.on("session_start", lambda ph, pl: hook_calls.append(ph))
            hooks.on("session_end", lambda ph, pl: hook_calls.append(ph))
            reg5 = ToolRegistry()
            reg5.register_function(lambda text: "ok", name="echo")
            router5 = ProviderRouter()
            router5.register_provider("mock", FinalAnswerProvider())
            router5.register_model(ModelRoute(id="final", provider_name="mock"))
            loop5 = AgentLoop(
                provider_router=router5,
                tool_registry=reg5,
                budget=IterationBudget(max_iterations=3),
                session_log=slog5,
                event_store=es,
                hooks=hooks,
            )
            res5 = await loop5.run(task="integración", session_id=sess5.id)
            has_events = es.count(sess5.id) >= 1
            has_trajectory = res5.trajectory is not None and len(res5.trajectory.steps) >= 2
            assert has_events and has_trajectory and "session_start" in hook_calls
            es.close()
        finally:
            slog5.close()

    asyncio.run(run())


def test_iteration_budget_tick():
    b = IterationBudget(max_iterations=3, max_tokens=100, max_tool_calls=5)
    b.tick()
    b.tick()
    assert b.iterations == 2 and not b.exhausted
    b.tick()
    assert b.exhausted and b.reason is not None


def test_error_classifier():
    c1 = classify_error("tool", "connection timeout")
    c2 = classify_error("tool", "permission denied for file")
    c3 = classify_error("tool", "invalid param value")
    assert c1 == ErrorAction.RETRY
    assert c2 == ErrorAction.ABORT
    assert c3 == ErrorAction.REPLAN


def test_parallel_prefix_stops_at_mutating_tool():
    """Un batch [write, read] debe ejecutar write ANTES de read.

    Regresion: el pre-dispatch paralelo tomaba TODAS las tools read-only del
    batch, aunque estuvieran despues de una mutante, y las corria antes.
    """
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="loop-order")
            order: list[str] = []
            reg = ToolRegistry()

            def write_file(path: str, content: str = ""):
                order.append("write")
                return "ok"

            def read_file(path: str):
                order.append("read")
                return "contenido"

            reg.register_function(write_file, name="write_file", description="write")
            reg.register_function(read_file, name="read_file", description="read")
            router = ProviderRouter()
            router.register_provider("mock", OrderedBatchProvider())
            router.register_model(ModelRoute(id="batch", provider_name="mock"))
            loop = AgentLoop(
                provider_router=router,
                tool_registry=reg,
                budget=IterationBudget(max_iterations=5),
            )
            res = await loop.run(task="escribe y luego lee", session_id=sess.id, hint="coding")
            assert res.success
            assert order == ["write", "read"], order
        finally:
            slog.close()

    asyncio.run(run())
