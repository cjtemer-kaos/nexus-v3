"""PASO 2 — plan-mode: controller + evento/proyección, exit con approval, fail-closed, policy en loop."""
from __future__ import annotations

import asyncio

import pytest

from src.core.agent_loop import AgentLoop
from src.core.approval_gate import ApprovalGate
from src.core.iteration_budget import IterationBudget
from src.core.plan_mode import PLAN_MODE_EVENT, PLAN_POLICY, PlanModeController, PlanModeState
from src.core.provider_base import BaseProvider, LLMResponse, ToolCall
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry


def test_enter_logs_plan_mode_event_and_projection():
    slog = SessionLog(":memory:")
    try:
        sess = slog.create_session(project="plan")
        ctl = PlanModeController(session_log=slog, session_id=sess.id)
        proj = ctl.enter("plan: hacer X en 2 pasos", session_id=sess.id)
        assert proj.active and proj.state == "planning"
        assert proj.plan_text.startswith("plan:")
        events = [m for m in slog.get_messages(sess.id, limit=100) if m.tool_name == PLAN_MODE_EVENT]
        assert len(events) >= 1
        proj2 = ctl.project()
        assert proj2.plan_event_count >= 1 and proj2.active
    finally:
        slog.close()


def test_exit_only_with_approval_keep_planning_stays():
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="plan2")
            gate = ApprovalGate()
            ctl = PlanModeController(session_log=slog, approval_gate=gate, session_id=sess.id)
            # Fuera de plan: falla.
            assert ctl.exit_plan_mode(approved=True) is False
            ctl.enter("plan A")
            # Sin approval: no sale. Keep planning (False): no sale.
            assert ctl.exit_plan_mode(approved=False) is False
            assert ctl.is_active()
            ctl.on_review_response(False)
            assert ctl.is_active() and ctl.state == PlanModeState.PLANNING
            # Review real + Approve → sale.
            req = await ctl.request_review()
            assert req.escalation_policy == "reject" and req.options == ["Approve", "Keep planning"]
            await gate.respond(req.id, approved=True)
            assert ctl.exit_plan_mode(approved=True, request_id=req.id) is True
            assert ctl.state == PlanModeState.OFF
        finally:
            slog.close()
    asyncio.run(run())


def test_reject_stays_in_plan():
    async def run():
        gate = ApprovalGate()
        ctl = PlanModeController(approval_gate=gate)
        ctl.enter("plan B")
        req = await ctl.request_review()
        await gate.respond(req.id, approved=False)
        ctl.on_review_response(False)
        assert ctl.is_active()
        # Aunque se pida salir en aprobado, el gate dice rechazado → no sale.
        assert ctl.exit_plan_mode(approved=True, request_id=req.id) is False
    asyncio.run(run())


def test_fail_closed_without_channel():
    async def run():
        ctl = PlanModeController()  # sin session_log ni approval_gate
        ctl.enter("plan C")
        assert ctl.is_active()
        with pytest.raises(RuntimeError):
            await ctl.request_review()
        # Sin canal jamás se sale en aprobado.
        assert ctl.exit_plan_mode(approved=True) is False
        assert ctl.is_active()
    asyncio.run(run())


def test_request_plan_review_forces_reject_escalation():
    async def run():
        gate = ApprovalGate()
        req = await gate.request_plan_review("mi plan", escalation="auto_approve")
        assert req.escalation_policy == "reject"
        assert req.kind == "plan-review"
    asyncio.run(run())


def test_loop_injects_policy_and_keeps_it_after_approve():
    class CaptureProvider(BaseProvider):
        def __init__(self):
            self.calls = 0
            self.seen = []

        async def chat(self, messages, tools=None, model=None, **kw):
            self.calls += 1
            self.seen.append([m for m in messages if m.role == "system"])
            if self.calls == 1 and tools:
                tc = ToolCall(id="c1", name=tools[0]["function"]["name"], arguments={"text": "x"})
                return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="m")
            return LLMResponse(content="plan listo", model="m", usage={"total_tokens": 5})

        async def health(self):
            return True

        async def list_models(self):
            return ["m"]

    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="plan3")
            gate = ApprovalGate()
            ctl = PlanModeController(session_log=slog, approval_gate=gate, session_id=sess.id)
            ctl.enter("plan D")
            prov = CaptureProvider()
            router = ProviderRouter()
            router.register_provider("mock", prov)
            router.register_model(ModelRoute(id="m", provider_name="mock"))
            reg = ToolRegistry()
            reg.register_function(lambda text: "ok", name="echo")
            loop = AgentLoop(provider_router=router, tool_registry=reg,
                             budget=IterationBudget(max_iterations=5),
                             session_log=slog)
            # Aprobar a mitad implicaría estado APPROVED pero la guía sigue en el batch.
            req = await ctl.request_review()
            await gate.respond(req.id, approved=True)
            ctl.on_review_response(True)
            res = await loop.run(task="planifica", session_id=sess.id, plan_mode=ctl)
            assert res.success
            # La guía plan:policy estuvo presente en TODAS las llamadas LLM del batch.
            assert prov.seen, "el provider no recibió llamadas"
            for systems in prov.seen:
                assert any("plan:policy" in (m.content or "") for m in systems), systems
            # plan:policy NO contamina el historial persistido como user/assistant.
            roles = [m.role for m in slog.get_messages(sess.id, limit=100)]
            assert "user" in roles and "assistant" in roles
        finally:
            slog.close()
    asyncio.run(run())


def test_loop_without_plan_mode_no_policy():
    class CaptureFinal(BaseProvider):
        def __init__(self):
            self.systems = None

        async def chat(self, messages, tools=None, model=None, **kw):
            self.systems = [m.content or "" for m in messages if m.role == "system"]
            return LLMResponse(content="ok", model="m")

        async def health(self):
            return True

        async def list_models(self):
            return ["m"]

    async def run():
        prov = CaptureFinal()
        router = ProviderRouter()
        router.register_provider("mock", prov)
        router.register_model(ModelRoute(id="m", provider_name="mock"))
        loop = AgentLoop(provider_router=router, tool_registry=ToolRegistry(),
                         budget=IterationBudget(max_iterations=2))
        await loop.run(task="hola", session_id="s-nopolicy")
        assert prov.systems is not None
        assert not any("plan:policy" in s for s in prov.systems)
        # exit_plan_mode del loop falla sin controller / fuera de plan.
        assert loop.exit_plan_mode(None, approved=True) is False
    asyncio.run(run())
    assert "plan:policy" in PLAN_POLICY
