"""
A2A Server — NEXUS como hub que recibe tareas de agentes externos.
Implementa Google A2A protocol (simplified).
Endpoints: /.well-known/agent.json (agent card), /a2a/tasks (create/get/cancel)
"""
from __future__ import annotations
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Awaitable
from src.core.nexus_config import get_nexus_url

logger = logging.getLogger(__name__)


class A2ATaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class A2AAgentCard:
    """Agent Card (.well-known/agent.json) — describes NEXUS capabilities."""
    name: str = "NEXUS Director"
    description: str = "Multi-agent AI orchestrator with persistent memory"
    url: str = field(default_factory=get_nexus_url)
    version: str = "2.0"
    capabilities: list[str] = field(default_factory=lambda: [
        "code", "research", "analysis", "refactor", "debug", "test",
        "deploy", "documentation", "memory", "learning",
    ])
    protocols: list[str] = field(default_factory=lambda: ["a2a", "mcp", "acp"])

    def to_dict(self) -> dict:
        return {
            "name": self.name, "description": self.description,
            "url": self.url, "version": self.version,
            "capabilities": {"streaming": False, "pushNotifications": False,
                           "skills": self.capabilities},
            "protocols": self.protocols,
        }


@dataclass
class A2ATask:
    id: str = field(default_factory=lambda: f"task-{uuid.uuid4().hex[:8]}")
    description: str = ""
    state: A2ATaskState = A2ATaskState.SUBMITTED
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = ""
    submitter: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "description": self.description[:200],
            "state": self.state.value, "input": self.input_data,
            "output": self.output_data, "error": self.error,
            "created_at": self.created_at, "submitter": self.submitter,
        }


class A2AServer:
    """Receives tasks from external agents via A2A protocol."""
    def __init__(self, executor: Callable[[str], Awaitable[dict]] | None = None):
        self._tasks: dict[str, A2ATask] = {}
        self._executor = executor
        self.agent_card = A2AAgentCard()

    def create_task(self, description: str, input_data: dict | None = None,
                    submitter: str = "") -> A2ATask:
        task = A2ATask(description=description, input_data=input_data or {},
                       submitter=submitter)
        self._tasks[task.id] = task
        return task

    def get_task(self, task_id: str) -> A2ATask | None:
        return self._tasks.get(task_id)

    def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task and task.state in (A2ATaskState.SUBMITTED, A2ATaskState.WORKING):
            task.state = A2ATaskState.CANCELED
            task.updated_at = datetime.now().isoformat()
            return True
        return False

    async def execute_task(self, task_id: str) -> A2ATask:
        task = self._tasks.get(task_id)
        if not task:
            raise KeyError(f"Task {task_id} not found")
        task.state = A2ATaskState.WORKING
        task.updated_at = datetime.now().isoformat()
        try:
            if self._executor:
                result = await self._executor(task.description)
                task.output_data = result
                task.state = A2ATaskState.COMPLETED
            else:
                task.state = A2ATaskState.FAILED
                task.error = "No executor configured"
        except Exception as e:
            task.state = A2ATaskState.FAILED
            task.error = str(e)
        task.updated_at = datetime.now().isoformat()
        return task

    def list_tasks(self, state: str | None = None, limit: int = 20) -> list[A2ATask]:
        tasks = list(self._tasks.values())
        if state:
            tasks = [t for t in tasks if t.state.value == state]
        return tasks[-limit:]

    def status(self) -> dict:
        by_state = {}
        for t in self._tasks.values():
            by_state[t.state.value] = by_state.get(t.state.value, 0) + 1
        return {"total_tasks": len(self._tasks), "by_state": by_state}
