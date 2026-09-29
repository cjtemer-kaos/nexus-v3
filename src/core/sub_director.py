"""
Sub-Directors — Coordinadores de dominio.

Cada Sub-Director gestiona un dominio (Code, Research, Ops, Voice)
con su propio budget de tokens y pool de agentes.

Cadena de mando: Director -> Sub-Director -> Agente.
El Sub-Director NO decide que hacer. Ejecuta ordenes del Director
dentro de su dominio, delegando a sus agentes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.command_protocol import Command, CommandResult, CommandStatus

logger = logging.getLogger(__name__)


@dataclass
class SubDirectorConfig:
    name: str
    domain: str
    capabilities: list[str]
    agents: list[str]  # agent names this sub-director can use
    token_budget: int = 50000
    escalation_threshold: int = 3  # consecutive failures before escalating to Director


@dataclass
class SubDirectorMetrics:
    commands_received: int = 0
    commands_completed: int = 0
    commands_failed: int = 0
    tokens_used: int = 0
    consecutive_failures: int = 0
    escalations: int = 0
    avg_duration_s: float = 0.0
    _total_duration: float = 0.0


class SubDirector:
    """Coordinador de dominio con budget y pool de agentes."""

    def __init__(self, config: SubDirectorConfig):
        self.config = config
        self.metrics = SubDirectorMetrics()
        self._budget_used = 0

    @property
    def remaining_budget(self) -> int:
        return max(0, self.config.token_budget - self._budget_used)

    @property
    def is_over_budget(self) -> bool:
        return self._budget_used >= self.config.token_budget

    def can_handle(self, command: Command) -> bool:
        """Puede este sub-director manejar este command?"""
        return command.target == f"sub-director-{self.config.name}"

    def consume_budget(self, tokens: int) -> None:
        self._budget_used += tokens

    def reset_budget(self) -> None:
        self._budget_used = 0

    def select_agent(self, command: Command) -> str:
        """Selecciona el mejor agente del pool para el command."""
        if not self.config.agents:
            return f"gema-{self.config.name}"
        # Simple: first agent. Could be enhanced with capability matching.
        task = command.instruction.get("task", "").lower()
        for agent in self.config.agents:
            # Extract agent type from name (e.g. "gema-debugger" -> "debug")
            agent_type = agent.replace("gema-", "").replace("sub-", "")
            if agent_type in task:
                return agent
        return self.config.agents[0]

    def record_result(self, result: CommandResult) -> None:
        self.metrics.commands_received += 1
        self.metrics.tokens_used += result.tokens_used
        self.metrics._total_duration += result.duration_s

        if result.status == CommandStatus.COMPLETED:
            self.metrics.commands_completed += 1
            self.metrics.consecutive_failures = 0
        else:
            self.metrics.commands_failed += 1
            self.metrics.consecutive_failures += 1

        total = self.metrics.commands_received
        self.metrics.avg_duration_s = self.metrics._total_duration / max(total, 1)

    def should_escalate(self) -> bool:
        return self.metrics.consecutive_failures >= self.config.escalation_threshold

    def status(self) -> dict:
        return {
            "name": self.config.name,
            "domain": self.config.domain,
            "agents": self.config.agents,
            "budget": {
                "total": self.config.token_budget,
                "used": self._budget_used,
                "remaining": self.remaining_budget,
                "over_budget": self.is_over_budget,
            },
            "metrics": {
                "received": self.metrics.commands_received,
                "completed": self.metrics.commands_completed,
                "failed": self.metrics.commands_failed,
                "tokens_used": self.metrics.tokens_used,
                "consecutive_failures": self.metrics.consecutive_failures,
                "escalations": self.metrics.escalations,
                "avg_duration_s": round(self.metrics.avg_duration_s, 2),
            },
        }


class SubDirectorRegistry:
    """Registry de todos los Sub-Directors."""

    def __init__(self):
        self._sub_directors: dict[str, SubDirector] = {}

    def register(self, sd: SubDirector) -> None:
        self._sub_directors[sd.config.name] = sd

    @property
    def sub_directors(self) -> list[SubDirector]:
        return list(self._sub_directors.values())

    def get(self, name: str) -> SubDirector | None:
        return self._sub_directors.get(name)

    def route(self, command: Command) -> SubDirector | None:
        for sd in self._sub_directors.values():
            if sd.can_handle(command):
                return sd
        return None

    def reset_all_budgets(self) -> None:
        for sd in self._sub_directors.values():
            sd.reset_budget()

    def status(self) -> dict:
        return {
            "sub_directors": {
                name: sd.status() for name, sd in self._sub_directors.items()
            },
        }

    @classmethod
    def create_defaults(cls) -> SubDirectorRegistry:
        registry = cls()
        defaults = [
            SubDirectorConfig(
                name="code",
                domain="code",
                capabilities=["code", "refactor", "debug", "test", "architect", "implement", "build"],
                agents=["gema-coder", "gema-debugger", "gema-architect", "gema-tester", "gema-optimizer"],
                token_budget=80000,
            ),
            SubDirectorConfig(
                name="research",
                domain="research",
                capabilities=["research", "search", "analyze", "investigate", "study", "learn"],
                agents=["gema-scholar", "gema-analyst", "gema-web-scraper"],
                token_budget=60000,
            ),
            SubDirectorConfig(
                name="ops",
                domain="ops",
                capabilities=["deploy", "devops", "docker", "ci", "cd", "monitor", "server", "infra"],
                agents=["gema-devops", "gema-sysadmin", "gema-monitor"],
                token_budget=40000,
            ),
            SubDirectorConfig(
                name="voice",
                domain="voice",
                capabilities=["voice", "stt", "tts", "audio", "speak", "listen", "ui", "design"],
                agents=["gema-voice", "gema-ui-designer"],
                token_budget=30000,
            ),
        ]
        for config in defaults:
            registry.register(SubDirector(config))
        return registry
