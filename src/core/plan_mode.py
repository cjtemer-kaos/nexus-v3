"""Plan-mode — modo planificación con aprobación humana (PASO 2).

- PlanModeController: entra/sale de modo plan, emite evento ``plan/mode`` en
  SessionLog (solo vía su API pública) y expone una proyección read-only.
- Salir SOLO con approval del ApprovalGate; "Keep planning" no sale.
- Fail-closed: sin canal de aprobación (approval_gate=None) no se puede
  pedir review ni salir en aprobado.
- agent_loop.run(..., plan_mode=...) inyecta la guía ``plan:policy`` mientras
  el modo está activo y la mantiene hasta fin de batch aunque se apruebe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

PLAN_MODE_EVENT = "plan/mode"

PLAN_POLICY = (
    "plan:policy — Estás en MODO PLAN. Solo planifica: analiza, descompón en "
    "pasos y propón el plan. NO ejecutes herramientas de escritura, "
    "modificación, red ni operaciones destructivas/gated. Si necesitas "
    "proponer acciones, descríbelas en el plan y pide revisión con "
    "request_plan_review. Solo sales de este modo con aprobación explícita."
)


class PlanModeState(str, Enum):
    OFF = "off"
    PLANNING = "planning"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"


@dataclass
class PlanModeProjection:
    active: bool
    state: str
    session_id: str
    plan_text: str = ""
    review_request_id: str = ""
    plan_event_count: int = 0
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "state": self.state,
            "session_id": self.session_id,
            "plan_text": self.plan_text,
            "review_request_id": self.review_request_id,
            "plan_event_count": self.plan_event_count,
            "updated_at": self.updated_at,
        }


class PlanModeController:
    """Controla el modo plan de una sesión. Fail-closed por defecto."""

    def __init__(self, session_log=None, approval_gate=None, session_id: str = ""):
        self.session_log = session_log
        self.approval_gate = approval_gate
        self.session_id = session_id
        self.state: PlanModeState = PlanModeState.OFF
        self.plan_text: str = ""
        self.review_request_id: str = ""

    # -- API pública ----------------------------------------------------
    def is_active(self) -> bool:
        return self.state in (PlanModeState.PLANNING, PlanModeState.AWAITING_REVIEW)

    def policy_applies(self, session_id: str = "") -> bool:
        """La guía plan:policy aplica mientras no se haya salido del modo plan.

        Approve la mantiene hasta fin de batch: solo OFF la retira. Si el
        controller está ligado a otra sesión, no aplica.
        """
        if self.state == PlanModeState.OFF:
            return False
        if self.session_id and session_id and self.session_id != session_id:
            return False
        return True

    def enter(self, plan_text: str = "", session_id: str | None = None) -> PlanModeProjection:
        if session_id:
            self.session_id = session_id
        self.state = PlanModeState.PLANNING
        self.plan_text = plan_text
        self.review_request_id = ""
        self._log_event(f"enter state=planning plan={plan_text[:500]}")
        return self.project()

    async def request_review(self, plan_text: str | None = None, timeout: int | None = None):
        """Pide revisión del plan vía approval_gate. Fail-closed sin canal."""
        if self.approval_gate is None:
            raise RuntimeError("plan-mode sin canal de aprobación: fail-closed, no se puede pedir review")
        if plan_text is not None:
            self.plan_text = plan_text
        self.state = PlanModeState.AWAITING_REVIEW
        req = await self.approval_gate.request_plan_review(self.plan_text, timeout=timeout)
        self.review_request_id = req.id
        self._log_event(f"review requested id={req.id}")
        return req

    def on_review_response(self, approved: bool) -> PlanModeProjection:
        """Aplica la respuesta humana: Approve → APPROVED; Keep planning (False) → sigue en plan."""
        if approved:
            self.state = PlanModeState.APPROVED
            self._log_event("review approved")
        else:
            # Keep planning: NO sale del modo plan.
            self.state = PlanModeState.PLANNING
            self._log_event("keep planning: sigue en modo plan")
        return self.project()

    def exit_plan_mode(self, approved: bool = False, request_id: str | None = None) -> bool:
        """Sale del modo plan SOLO con approval verificada en el gate.

        - Fuera de plan (OFF) → False (falla).
        - Sin canal → False (fail-closed).
        - approved=False (Keep planning) → False, no sale.
        - approved=True exige request aprobada en el gate → True y pasa a OFF.
        """
        if self.state == PlanModeState.OFF:
            return False
        if not approved:
            return False
        if self.approval_gate is None:
            return False
        rid = request_id or self.review_request_id
        if not rid:
            return False
        try:
            req = self.approval_gate.get_request(rid)
        except Exception:
            return False
        if req is None or getattr(req.status, "value", req.status) != "approved":
            return False
        self.state = PlanModeState.OFF
        self.review_request_id = ""
        self._log_event("exit approved: fuera de modo plan")
        return True

    def project(self) -> PlanModeProjection:
        """Proyección read-only del modo plan (incluye conteo de eventos plan/mode)."""
        return PlanModeProjection(
            active=self.is_active(),
            state=self.state.value,
            session_id=self.session_id,
            plan_text=self.plan_text,
            review_request_id=self.review_request_id,
            plan_event_count=self._count_plan_events(),
        )

    # -- internos (SessionLog solo como API: append/get) -----------------
    def _log_event(self, content: str) -> None:
        if self.session_log is None or not self.session_id:
            return
        try:
            from .session_log import Message
            self.session_log.append(
                self.session_id,
                Message(role="system", content=f"[{PLAN_MODE_EVENT} {content}]", tool_name=PLAN_MODE_EVENT),
            )
        except Exception:
            pass

    def _count_plan_events(self) -> int:
        if self.session_log is None or not self.session_id:
            return 0
        try:
            msgs = self.session_log.get_messages(self.session_id, limit=10000)
            return sum(1 for m in msgs if getattr(m, "tool_name", "") == PLAN_MODE_EVENT)
        except Exception:
            return 0

    def policy_guide(self) -> str:
        return PLAN_POLICY
