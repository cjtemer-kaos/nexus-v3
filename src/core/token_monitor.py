"""
Token Monitor — track token usage per agent, alert/budget management.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    agent: str
    tokens: int
    timestamp: float = field(default_factory=time.time)
    operation: str = ""


@dataclass
class BudgetAlert:
    agent: str
    level: str  # warning, critical, block
    usage: int
    budget: int
    timestamp: float = field(default_factory=time.time)


class TokenMonitor:
    """Track token usage per agent and alert when approaching budget."""

    def __init__(self):
        self._records: list[UsageRecord] = []
        self._alerts: list[BudgetAlert] = []
        self._budgets: dict[str, int] = {}  # agent -> max tokens
        self._warning_pct = 0.8
        self._block_pct = 0.95

    def set_budget(self, agent: str, max_tokens: int) -> None:
        self._budgets[agent] = max_tokens

    def record_usage(self, agent: str, tokens: int, operation: str = "") -> BudgetAlert | None:
        """Record token usage and return alert if budget exceeded thresholds."""
        self._records.append(UsageRecord(agent=agent, tokens=tokens, operation=operation))
        if len(self._records) > 10000:
            self._records = self._records[-5000:]

        budget = self._budgets.get(agent)
        if budget is None:
            return None

        total = self.get_usage(agent)
        ratio = total / budget

        if ratio >= self._block_pct:
            alert = BudgetAlert(agent=agent, level="block", usage=total, budget=budget)
        elif ratio >= self._warning_pct:
            alert = BudgetAlert(agent=agent, level="warning", usage=total, budget=budget)
        else:
            return None

        self._alerts.append(alert)
        if len(self._alerts) > 500:
            self._alerts = self._alerts[-250:]
        return alert

    def get_usage(self, agent: str, since: float = 0.0) -> int:
        """Get total tokens used by an agent."""
        return sum(
            r.tokens for r in self._records
            if r.agent == agent and r.timestamp >= since
        )

    def check_budget(self, agent: str) -> dict:
        """Check if agent can still use tokens."""
        budget = self._budgets.get(agent)
        if budget is None:
            return {"allowed": True, "budget": None, "usage": 0}
        usage = self.get_usage(agent)
        ratio = usage / budget if budget > 0 else 1.0
        return {
            "allowed": ratio < self._block_pct,
            "budget": budget,
            "usage": usage,
            "ratio": round(ratio, 3),
            "warning": ratio >= self._warning_pct,
        }

    def alerts(self, agent: str | None = None, level: str | None = None) -> list[BudgetAlert]:
        """Get alerts, optionally filtered."""
        results = self._alerts
        if agent:
            results = [a for a in results if a.agent == agent]
        if level:
            results = [a for a in results if a.level == level]
        return results

    def status(self) -> dict:
        agents = {}
        for r in self._records:
            if r.agent not in agents:
                agents[r.agent] = {"total_tokens": 0, "calls": 0}
            agents[r.agent]["total_tokens"] += r.tokens
            agents[r.agent]["calls"] += 1
        return {
            "agents": agents,
            "total_records": len(self._records),
            "alerts_count": len(self._alerts),
            "budgets_configured": len(self._budgets),
        }
