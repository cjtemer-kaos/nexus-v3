"""PASO 1 — trajectory base: orden de eventos, duration>=0, resume closers, max-tokens sin assistant vacío."""
from __future__ import annotations

import asyncio

from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.tool_registry import ToolRegistry
from src.core.trajectory import Trajectory, TrajectoryStep
from helpers import ToolThenFinalProvider, FinalAnswerProvider


def _router(provider: BaseProvider, model_id: str = "m") -> ProviderRouter:
    r = ProviderRouter()
    r.register_provider("mock", provider)
    r.register_model(ModelRoute(id=model_id, provider_name="mock"))
    return r


def _kinds(traj: Trajectory) -> list[str]:
    return [s.step or s.role for s in traj.steps]


def test_event_order_per_turn():
    async def run():
        reg = ToolRegistry()
        reg.register_function(lambda text: f"echo:{text}", name="echo", description="echo")
        loop = AgentLoop(provider_router=_router(ToolThenFinalProvider()), tool_registry=reg,
                         budget=IterationBudget(max_iterations=5))
        res = await loop.run(task="usa echo", session_id="s-order")
        kinds = _kinds(res.trajectory)
        # [user] + turno 1: turn/start → step/start → assistant → tool → step/end → turn/end
        assert kinds[:7] == ["user", "turn/start", "step/start", "assistant", "tool", "step/end", "turn/end"], kinds[:7]
        # Turno 2 (respuesta final): turn/start → step/start → assistant → step/end → turn/end
        assert kinds[7:12] == ["turn/start", "step/start", "assistant", "step/end", "turn/end"], kinds[7:]
        assert res.success
    asyncio.run(run())


def test_duration_non_negative_and_seq_monotonic():
    async def run():
        reg = ToolRegistry()
        reg.register_function(lambda text: "ok", name="echo")
        loop = AgentLoop(provider_router=_router(FinalAnswerProvider()), tool_registry=reg,
                         budget=IterationBudget(max_iterations=3))
        res = await loop.run(task="hola", session_id="s-dur")
        seqs = [s.seq for s in res.trajectory.steps]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert all((s.duration_ms or 0) >= 0 for s in res.trajectory.steps)
        assert all((s.time_ms or 0) > 0 for s in res.trajectory.steps)
    asyncio.run(run())


def test_resume_inserts_closers():
    async def run():
        traj = Trajectory(session_id="s-resume", task="t")
        traj.append_event("turn/start", content="turno 1", iteration=1, turn=1)
        traj.append_assistant("a medias", iteration=1, turn=1)
        # Sin turn/end: turno interrumpido.
        reg = ToolRegistry()
        loop = AgentLoop(provider_router=_router(FinalAnswerProvider()), tool_registry=reg,
                         budget=IterationBudget(max_iterations=3))
        res = await loop.run(task="continuar", session_id="s-resume", trajectory=traj)
        kinds = _kinds(res.trajectory)
        # Closers de resume antes del user + primer turn/start nuevo.
        assert kinds[2] == "step/end" and res.trajectory.steps[2].end_reason == "resume-close", kinds[:6]
        assert kinds[3] == "turn/end" and res.trajectory.steps[3].end_reason == "resume-close", kinds[:6]
        assert kinds[4] == "user" and kinds[5] == "turn/start", kinds[:6]
    asyncio.run(run())


def test_no_duplicate_closers_when_closed():
    traj = Trajectory(session_id="s-closed", task="t")
    traj.append_event("turn/start", iteration=1, turn=1)
    traj.append_event("turn/end", iteration=1, turn=1)
    closers = traj.close_interrupted_turns()
    assert closers == []


def test_max_tokens_no_empty_assistant():
    class MaxTokensProvider(BaseProvider):
        async def chat(self, messages, tools=None, model=None, **kw):
            return LLMResponse(content="", finish_reason="length", model="m", usage={"total_tokens": 5})

        async def health(self):
            return True

        async def list_models(self):
            return ["m"]

    async def run():
        reg = ToolRegistry()
        loop = AgentLoop(provider_router=_router(MaxTokensProvider()), tool_registry=reg,
                         budget=IterationBudget(max_iterations=3))
        res = await loop.run(task="corta por tokens", session_id="s-maxtok")
        assistants = [s for s in res.trajectory.steps if s.role == "assistant"]
        assert assistants == [], [s.to_dict() for s in assistants]
        ends = [s for s in res.trajectory.steps if s.step == "turn/end"]
        assert ends and ends[-1].end_reason == "max_tokens"
    asyncio.run(run())


def test_unified_schema_roundtrip():
    s = TrajectoryStep(iteration=2, role="assistant", content="hola", turn=2, step="assistant",
                       tokens=7, model="m", duration_ms=3.5, end_reason="stop")
    d = s.to_dict()
    back = TrajectoryStep.from_dict(d)
    assert (back.turn, back.step, back.tokens, back.model, back.end_reason) == (2, "assistant", 7, "m", "stop")
    assert back.duration_ms >= 0
    proj = s.to_loop_step()
    assert proj["phase"] == "assistant" and proj["tokens"] == 7
