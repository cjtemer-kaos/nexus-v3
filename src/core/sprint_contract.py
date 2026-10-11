"""SprintContract — done_condition + takeover al Director (PASO 3).

Enlazado a IterationBudget: el contrato puede atarse a un budget y, si el
budget se agota o los errores llegan a max_errors (default 3), el sprint se
entrega (takeover) al Director vía callback en vez de seguir reintentando.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .iteration_budget import IterationBudget

CONTINUE = "continue"
DONE = "done"
TAKEOVER = "takeover"


@dataclass
class SprintContract:
    done_condition: str = ""
    max_errors: int = 3
    errors: int = 0
    takeovers: int = 0
    completed: bool = False
    on_takeover: Callable[..., Any] | None = None
    budget: IterationBudget | None = None
    last_reason: str = ""

    def bind_budget(self, budget: IterationBudget) -> "SprintContract":
        """Enlaza el contrato a un IterationBudget (takeover si se agota)."""
        self.budget = budget
        return self

    def is_done(self, output: str) -> bool:
        """True si el output satisface la done_condition (substring, case-insensitive)."""
        if not self.done_condition:
            return False
        return self.done_condition.lower() in (output or "").lower()

    def record_success(self, output: str = "") -> str:
        if self.is_done(output):
            self.completed = True
            self.last_reason = "done_condition"
            return DONE
        return self.check_budget()

    def record_error(self, error: str = "") -> str:
        self.errors += 1
        self.last_reason = f"error:{str(error)[:200]}"
        if self.errors >= self.max_errors:
            return self.takeover(f"max_errors {self.max_errors} alcanzado")
        return self.check_budget()

    def check_budget(self) -> str:
        """Si el budget enlazado está agotado → takeover al Director."""
        if self.budget is not None and self.budget.exhausted:
            return self.takeover(f"budget agotado: {self.budget.reason}")
        return CONTINUE

    def takeover(self, reason: str = "") -> str:
        self.takeovers += 1
        self.last_reason = reason or self.last_reason
        if self.on_takeover is not None:
            try:
                self.on_takeover(self, reason)
            except Exception:
                pass
        return TAKEOVER

    def to_dict(self) -> dict:
        return {
            "done_condition": self.done_condition,
            "max_errors": self.max_errors,
            "errors": self.errors,
            "takeovers": self.takeovers,
            "completed": self.completed,
            "last_reason": self.last_reason,
            "budget": self.budget.to_dict() if self.budget else None,
        }
