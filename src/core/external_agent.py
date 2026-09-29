"""
External Agent Registry — Conecta cualquier agente IA externo a NEXUS.

El Director trata agentes externos IGUAL que gemas internas.
Solo necesita: capability, protocol, cost, reliability.
El adapter traduce el Command Protocol al protocolo del agente.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.core.command_protocol import Command, CommandResult, CommandStatus

logger = logging.getLogger(__name__)


@dataclass
class ExternalAgent:
    name: str
    capabilities: list[str]
    protocol: str  # "cli", "http", "mcp", "a2a", "stdin", "messageboard", "mock"
    endpoint: str  # comando CLI, URL, socket path
    cost: str = "free"  # "free", "token-based", "api-credits"
    reliability: float = 1.0  # 0.0-1.0, updated by history
    max_concurrent: int = 1
    timeout_s: int = 300
    metadata: dict[str, Any] = field(default_factory=dict)
    adapter: AgentAdapter | None = None  # adapter instance, assigned at runtime
    _active_tasks: int = 0  # runtime counter, not serialized

    def capability_score(self, task: str) -> int:
        task_lower = task.lower()
        task_words = set(re.findall(r'\w+', task_lower))
        return sum(1 for cap in self.capabilities if cap in task_words or any(cap in w for w in task_words))


class ExternalAgentRegistry:
    """Registry de agentes externos con selection inteligente."""

    def __init__(self):
        self._agents: dict[str, ExternalAgent] = {}
        self._history: dict[str, list[bool]] = {}  # name -> [success, success, fail, ...]

    def register(self, agent: ExternalAgent) -> None:
        self._agents[agent.name] = agent
        if agent.name not in self._history:
            self._history[agent.name] = []

    def register_with_adapter(self, agent: ExternalAgent, adapter: AgentAdapter) -> None:
        agent.adapter = adapter
        self.register(agent)

    def unregister(self, name: str) -> None:
        self._agents.pop(name, None)

    def get(self, name: str) -> ExternalAgent | None:
        return self._agents.get(name)

    @property
    def agents(self) -> list[ExternalAgent]:
        return list(self._agents.values())

    def acquire(self, name: str) -> bool:
        agent = self._agents.get(name)
        if agent and agent._active_tasks < agent.max_concurrent:
            agent._active_tasks += 1
            return True
        return False

    def release(self, name: str) -> None:
        agent = self._agents.get(name)
        if agent and agent._active_tasks > 0:
            agent._active_tasks -= 1

    def best_for(self, task: str, prefer_free: bool = True) -> ExternalAgent | None:
        """Selecciona mejor agente: capability_match * reliability * cost_preference."""
        if not self._agents:
            return None

        candidates = []
        for agent in self._agents.values():
            if agent._active_tasks >= agent.max_concurrent:
                continue
            cap_score = agent.capability_score(task)
            if cap_score == 0:
                continue
            cost_bonus = 1.5 if (prefer_free and agent.cost == "free") else 1.0
            score = cap_score * agent.reliability * cost_bonus
            candidates.append((agent, score))

        if not candidates:
            return None
        candidates.sort(key=lambda x: -x[1])
        return candidates[0][0]

    def record_result(self, name: str, success: bool) -> None:
        """Actualiza reliability basado en historial."""
        if name not in self._history:
            self._history[name] = []
        self._history[name].append(success)
        # Keep last 50 results
        if len(self._history[name]) > 50:
            self._history[name] = self._history[name][-50:]
        # Update reliability
        agent = self._agents.get(name)
        if agent and self._history[name]:
            h = self._history[name]
            agent.reliability = sum(1 for s in h if s) / len(h)

    def status(self) -> dict:
        return {
            "total_agents": len(self._agents),
            "agents": {
                a.name: {
                    "capabilities": a.capabilities,
                    "protocol": a.protocol,
                    "cost": a.cost,
                    "reliability": round(a.reliability, 2),
                    "history_len": len(self._history.get(a.name, [])),
                }
                for a in self._agents.values()
            },
        }


# --- Adapters ---

class AgentAdapter(ABC):
    @abstractmethod
    async def send_command(self, command: Command) -> CommandResult:
        """Traduce Command a protocolo del agente y espera resultado."""


class CLIAdapter(AgentAdapter):
    """Para agentes CLI: claude-code, opencode, aider."""

    def __init__(self, binary: str, timeout_s: int = 300):
        self.binary = binary
        self.timeout_s = timeout_s

    async def send_command(self, command: Command) -> CommandResult:
        task = command.instruction.get("task", "")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "--message", task,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_s
            )
            output = stdout.decode("utf-8", errors="replace")
            if proc.returncode == 0:
                return CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.COMPLETED,
                    output=output,
                )
            else:
                return CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.FAILED,
                    output=output,
                    error=stderr.decode("utf-8", errors="replace")[:1000],
                )
        except asyncio.TimeoutError:
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.TIMEOUT,
                error=f"CLI timeout after {self.timeout_s}s",
            )
        except Exception as e:
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                error=str(e),
            )


class HTTPAdapter(AgentAdapter):
    """Para agentes HTTP: antigravity, agent-zero, APIs custom."""

    def __init__(self, endpoint: str, timeout_s: int = 300):
        self.endpoint = endpoint
        self.timeout_s = timeout_s

    async def send_command(self, command: Command) -> CommandResult:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "message": command.instruction.get("task", ""),
                    "command": command.to_dict(),
                }
                async with session.post(
                    self.endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_s),
                ) as resp:
                    data = await resp.json()
                    if resp.status == 200:
                        return CommandResult(
                            command_id=command.command_id,
                            status=CommandStatus.COMPLETED,
                            output=data.get("response", data.get("output", str(data))),
                            tokens_used=data.get("tokens_used", 0),
                        )
                    else:
                        return CommandResult(
                            command_id=command.command_id,
                            status=CommandStatus.FAILED,
                            error=f"HTTP {resp.status}: {data}",
                        )
        except Exception as e:
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                error=str(e),
            )


class MessageBoardAdapter(AgentAdapter):
    """Para agentes NEXUS internos via message_board.db."""

    def __init__(self, agent_name: str, poll_interval: float = 2.0, timeout_s: int = 300):
        self.agent_name = agent_name
        self.poll_interval = poll_interval
        self.timeout_s = timeout_s

    async def send_command(self, command: Command) -> CommandResult:
        # Import here to avoid circular deps
        try:
            from src.core.nexus_hive import NexusHive
            hive = NexusHive()
            # Send command as message
            hive.send_message(
                sender="director",
                target=self.agent_name,
                content=json.dumps(command.to_dict()),
                channel="commands",
            )
            # Poll for response
            import time
            deadline = time.time() + self.timeout_s
            while time.time() < deadline:
                msgs = hive.read_messages(channel="command_results", limit=20)
                for msg in msgs:
                    try:
                        data = json.loads(msg.get("content", "{}"))
                        if data.get("command_id") == command.command_id:
                            return CommandResult(
                                command_id=command.command_id,
                                status=CommandStatus(data.get("status", "completed")),
                                output=data.get("output", ""),
                                tokens_used=data.get("tokens_used", 0),
                                error=data.get("error"),
                            )
                    except (json.JSONDecodeError, ValueError):
                        continue
                await asyncio.sleep(self.poll_interval)

            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.TIMEOUT,
                error=f"No response from {self.agent_name} after {self.timeout_s}s",
            )
        except Exception as e:
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                error=str(e),
            )


class MockAdapter(AgentAdapter):
    """Para testing."""

    def __init__(self, response_output: str = "mock done", response_status: CommandStatus = CommandStatus.COMPLETED):
        self.response_output = response_output
        self.response_status = response_status

    async def send_command(self, command: Command) -> CommandResult:
        return CommandResult(
            command_id=command.command_id,
            status=self.response_status,
            output=self.response_output,
            tokens_used=50,
        )
