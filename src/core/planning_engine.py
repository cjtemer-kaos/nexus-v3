"""
Planning Engine - Devin/Claude Code-style plan management for SuperNEXUS v2.

Provides structured planning with steps, dependencies, status tracking,
and markdown import/export.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class PlanStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class PlanStep:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    description: str = ""
    status: StepStatus = StepStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    result: Optional[str] = None
    estimated_time: Optional[float] = None  # seconds


@dataclass
class Plan:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    status: PlanStatus = PlanStatus.DRAFT
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def _touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


class PlanningEngine:
    """Singleton planning engine for creating, managing, and exporting plans."""

    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}
        self._active_plan_id: Optional[str] = None

    # --- CRUD ---

    def create_plan(
        self, goal: str, steps: Optional[list[PlanStep]] = None
    ) -> Plan:
        plan = Plan(goal=goal, steps=steps or [])
        self._plans[plan.id] = plan
        if self._active_plan_id is None:
            self._active_plan_id = plan.id
            plan.status = PlanStatus.ACTIVE
        return plan

    def get_plan(self, plan_id: str) -> Plan:
        if plan_id not in self._plans:
            raise KeyError(f"Plan {plan_id!r} not found")
        return self._plans[plan_id]

    def get_active_plan(self) -> Optional[Plan]:
        if self._active_plan_id and self._active_plan_id in self._plans:
            return self._plans[self._active_plan_id]
        return None

    def update_step_status(
        self,
        plan_id: str,
        step_id: str,
        status: StepStatus,
        result: Optional[str] = None,
    ) -> PlanStep:
        plan = self.get_plan(plan_id)
        step = self._find_step(plan, step_id)
        step.status = status
        if result is not None:
            step.result = result
        plan._touch()
        self._auto_update_plan_status(plan)
        return step

    def add_step(self, plan_id: str, step: PlanStep) -> PlanStep:
        plan = self.get_plan(plan_id)
        plan.steps.append(step)
        plan._touch()
        return step

    def remove_step(self, plan_id: str, step_id: str) -> None:
        plan = self.get_plan(plan_id)
        before = len(plan.steps)
        plan.steps = [s for s in plan.steps if s.id != step_id]
        if len(plan.steps) == before:
            raise KeyError(f"Step {step_id!r} not found in plan {plan_id!r}")
        for s in plan.steps:
            s.depends_on = [d for d in s.depends_on if d != step_id]
        plan._touch()

    def reorder_steps(self, plan_id: str, step_ids: list[str]) -> None:
        plan = self.get_plan(plan_id)
        id_map = {s.id: s for s in plan.steps}
        ordered: list[PlanStep] = []
        for sid in step_ids:
            if sid in id_map:
                ordered.append(id_map[sid])
        mentioned = set(step_ids)
        for s in plan.steps:
            if s.id not in mentioned:
                ordered.append(s)
        plan.steps = ordered
        plan._touch()

    # --- Export / Import ---

    def export_plan(self, plan_id: str) -> str:
        plan = self.get_plan(plan_id)
        lines: list[str] = []
        lines.append(f"# Plan: {plan.goal}")
        lines.append("")
        lines.append(f"**Status:** {plan.status.value}  ")
        lines.append(f"**Created:** {plan.created_at}  ")
        lines.append(f"**Updated:** {plan.updated_at}")
        lines.append("")
        lines.append("## Steps")
        lines.append("")
        for step in plan.steps:
            check = "x" if step.status in (StepStatus.DONE, StepStatus.SKIPPED) else " "
            lines.append(f"- [{check}] {step.description}")
            if step.result:
                lines.append(f"  - Result: {step.result}")
            if step.estimated_time is not None:
                lines.append(f"  - Est. time: {step.estimated_time}s")
            if step.depends_on:
                lines.append(f"  - Depends on: {', '.join(step.depends_on)}")
        return "\n".join(lines) + "\n"

    def import_plan(self, markdown_str: str) -> Plan:
        lines = markdown_str.strip().splitlines()
        goal = ""
        steps: list[PlanStep] = []
        status = PlanStatus.DRAFT

        for line in lines:
            m_header = re.match(r"^#\s+Plan:\s*(.+)$", line)
            if m_header:
                goal = m_header.group(1).strip()
                continue

            m_status = re.match(r"^\*\*Status:\*\*\s*(\w+)", line)
            if m_status:
                try:
                    status = PlanStatus(m_status.group(1))
                except ValueError:
                    pass
                continue

            m_step = re.match(r"^- \[([ xX])\]\s+(.+)$", line)
            if m_step:
                done = m_step.group(1).lower() == "x"
                steps.append(
                    PlanStep(
                        description=m_step.group(2).strip(),
                        status=StepStatus.DONE if done else StepStatus.PENDING,
                    )
                )

        plan = Plan(goal=goal or "Imported Plan", steps=steps, status=status)
        self._plans[plan.id] = plan
        if self._active_plan_id is None:
            self._active_plan_id = plan.id
            plan.status = PlanStatus.ACTIVE
        return plan

    # --- Stats ---

    def get_stats(self) -> dict:
        total = len(self._plans)
        by_status: dict[str, int] = {}
        total_steps = 0
        for p in self._plans.values():
            by_status[p.status.value] = by_status.get(p.status.value, 0) + 1
            total_steps += len(p.steps)
        return {
            "total_plans": total,
            "active_plan_id": self._active_plan_id,
            "by_status": by_status,
            "total_steps": total_steps,
        }

    # --- Helpers ---

    @staticmethod
    def _find_step(plan: Plan, step_id: str) -> PlanStep:
        for s in plan.steps:
            if s.id == step_id:
                return s
        raise KeyError(f"Step {step_id!r} not found in plan {plan.id!r}")

    @staticmethod
    def _auto_update_plan_status(plan: Plan) -> None:
        if plan.status in (PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.DRAFT):
            return
        statuses = [s.status for s in plan.steps]
        if all(s == StepStatus.DONE or s == StepStatus.SKIPPED for s in statuses):
            plan.status = PlanStatus.COMPLETED
        elif any(s == StepStatus.FAILED for s in statuses):
            plan.status = PlanStatus.FAILED
        plan._touch()


# --- Singleton ---

_planner_instance: Optional[PlanningEngine] = None


def get_planner() -> PlanningEngine:
    global _planner_instance
    if _planner_instance is None:
        _planner_instance = PlanningEngine()
    return _planner_instance
