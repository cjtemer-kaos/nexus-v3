"""
Command Protocol — Contrato formal Director <-> Agentes.

Director ORDENA via Command. Agente REPORTA via CommandResult.
Sin negociacion, sin interpretacion. El agente ejecuta al pie de la letra.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class CommandStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


@dataclass
class Command:
    """Orden del Director a un agente."""
    target: str
    action: str  # "execute", "analyze", "test", "review", "search"
    instruction: dict[str, Any]
    command_id: str = field(default_factory=lambda: f"cmd-{uuid.uuid4().hex[:8]}")
    constraints: list[str] = field(default_factory=list)
    output_format: str = "text"  # "text", "json", "diff", "code"
    deadline_tokens: int = 10000
    priority: int = 3  # 1=critical, 5=low
    on_fail: str = "report_and_wait"  # "report_and_wait", "retry", "skip", "escalate"
    quality_gate: str = ""  # "judge_pipeline_L0", "judge_pipeline_L2", etc.
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "target": self.target,
            "action": self.action,
            "instruction": self.instruction,
            "constraints": self.constraints,
            "output_format": self.output_format,
            "deadline_tokens": self.deadline_tokens,
            "priority": self.priority,
            "on_fail": self.on_fail,
            "quality_gate": self.quality_gate,
            "created_at": self.created_at,
        }


@dataclass
class CommandResult:
    """Respuesta de un agente al Director."""
    command_id: str
    status: CommandStatus
    output: str = ""
    tokens_used: int = 0
    error: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0
    completed_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "status": self.status.value,
            "output": self.output[:5000],
            "tokens_used": self.tokens_used,
            "error": self.error,
            "duration_s": round(self.duration_s, 2),
        }


CommandHandler = Callable[[Command], Awaitable[CommandResult]]


class CommandDispatcher:
    """Despacha Commands al handler registrado para cada target."""

    def __init__(self, default_timeout_s: float = 300.0):
        self._handlers: dict[str, CommandHandler] = {}
        self._default_timeout = default_timeout_s
        self._history: list[dict] = []

    def register(self, target: str, handler: CommandHandler) -> None:
        self._handlers[target] = handler

    def unregister(self, target: str) -> None:
        self._handlers.pop(target, None)

    @property
    def targets(self) -> list[str]:
        return list(self._handlers.keys())

    async def dispatch(self, command: Command) -> CommandResult:
        """Despacha un Command al handler correspondiente."""
        handler = self._handlers.get(command.target)
        if not handler:
            result = CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                error=f"Unknown target: '{command.target}'. Available: {self.targets}",
            )
            self._record(command, result)
            return result

        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                handler(command),
                timeout=self._default_timeout,
            )
            result.duration_s = time.time() - t0
        except asyncio.TimeoutError:
            result = CommandResult(
                command_id=command.command_id,
                status=CommandStatus.TIMEOUT,
                error=f"Timeout after {self._default_timeout}s",
                duration_s=time.time() - t0,
            )
        except Exception as e:
            result = CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                error=str(e),
                duration_s=time.time() - t0,
            )
            logger.exception(f"Command {command.command_id} to {command.target} failed")

        # Process on_fail policy
        if result.status in (CommandStatus.FAILED, CommandStatus.TIMEOUT):
            result = await self._handle_on_fail(command, result)

        # Evaluate quality gate if specified
        if command.quality_gate and result.status == CommandStatus.COMPLETED:
            result = await self._evaluate_quality_gate(command, result)

        self._record(command, result)
        return result

    async def _evaluate_quality_gate(self, command: Command, result: CommandResult) -> CommandResult:
        gate = command.quality_gate
        if gate == "judge_pipeline_L0":
            if not result.output or not result.output.strip():
                result.status = CommandStatus.FAILED
                result.error = "Quality gate L0: empty output"
        elif gate == "judge_pipeline_L2":
            if len(result.output.strip()) < 50:
                result.status = CommandStatus.FAILED
                result.error = "Quality gate L2: output too short"
        return result

    async def _handle_on_fail(self, command: Command, result: CommandResult) -> CommandResult:
        """Process on_fail policy after a failed command."""
        policy = command.on_fail
        if policy == "retry":
            handler = self._handlers.get(command.target)
            if handler:
                logger.info(f"on_fail=retry: retrying {command.command_id} to {command.target}")
                try:
                    retry_result = await asyncio.wait_for(handler(command), timeout=self._default_timeout)
                    if retry_result.status == CommandStatus.COMPLETED:
                        return retry_result
                except Exception:
                    pass  # retry failed, keep original result
        elif policy == "escalate":
            logger.warning(f"on_fail=escalate: {command.command_id} escalated to director")
            result.metadata = result.artifacts  # preserve context
            result.error = f"ESCALATED: {result.error}"
        elif policy == "skip":
            logger.info(f"on_fail=skip: {command.command_id} skipped")
            result.status = CommandStatus.COMPLETED
            result.output = f"[SKIPPED] {result.error}"
            result.error = None
        # "report_and_wait" is default — just return the failure as-is
        return result

    async def dispatch_batch(self, commands: list[Command], max_parallel: int = 3) -> list[CommandResult]:
        """Despacha multiples commands en paralelo respetando max_parallel."""
        semaphore = asyncio.Semaphore(max_parallel)

        async def limited(cmd):
            async with semaphore:
                return await self.dispatch(cmd)

        return await asyncio.gather(*[limited(c) for c in commands])

    def _record(self, command: Command, result: CommandResult) -> None:
        self._history.append({
            "command_id": command.command_id,
            "target": command.target,
            "action": command.action,
            "status": result.status.value,
            "tokens": result.tokens_used,
            "duration_s": result.duration_s,
            "error": result.error,
            "ts": datetime.now().isoformat(),
        })
        # Keep last 200
        if len(self._history) > 200:
            self._history = self._history[-200:]

    def status(self) -> dict:
        total = len(self._history)
        completed = sum(1 for h in self._history if h["status"] == "completed")
        failed = sum(1 for h in self._history if h["status"] in ("failed", "timeout"))
        return {
            "registered_targets": self.targets,
            "total_commands": total,
            "completed": completed,
            "failed": failed,
            "success_rate": round(completed / max(total, 1), 2),
            "recent": self._history[-5:],
        }
