"""
Task Lifecycle - State machine for task execution with retry logic.
Absorbed from multica pattern — names cleaned.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorClass(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


TRANSIENT_PATTERNS = ["timeout", "connection", "rate limit", "temporary", "503", "429", "502", "504"]
PERMANENT_PATTERNS = ["not found", "invalid", "unauthorized", "forbidden", "404", "401", "403", "syntax error"]


def classify_error(error_msg: str) -> ErrorClass:
    lower = error_msg.lower()
    for p in PERMANENT_PATTERNS:
        if p in lower:
            return ErrorClass.PERMANENT
    for p in TRANSIENT_PATTERNS:
        if p in lower:
            return ErrorClass.TRANSIENT
    return ErrorClass.UNKNOWN


@dataclass
class TaskLifecycle:
    task_id: str
    status: TaskStatus = TaskStatus.QUEUED
    payload: Dict = field(default_factory=dict)
    result: Any = None
    error: str = ""
    error_class: ErrorClass = ErrorClass.UNKNOWN
    created_at: float = field(default_factory=time.time)
    dispatched_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    retry_count: int = 0
    max_retries: int = 3
    retry_delay: float = 1.0

    @property
    def elapsed(self) -> float:
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        elif self.started_at:
            return time.time() - self.started_at
        return 0.0

    @property
    def should_retry(self) -> bool:
        if self.retry_count >= self.max_retries:
            return False
        if self.error_class == ErrorClass.PERMANENT:
            return False
        return self.error_class in (ErrorClass.TRANSIENT, ErrorClass.UNKNOWN)


class TaskStateMachine:
    """Manage task lifecycle transitions with retry logic."""

    VALID_TRANSITIONS = {
        TaskStatus.QUEUED: [TaskStatus.DISPATCHED, TaskStatus.CANCELLED],
        TaskStatus.DISPATCHED: [TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED],
        TaskStatus.RUNNING: [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED],
        TaskStatus.COMPLETED: [],
        TaskStatus.FAILED: [],
        TaskStatus.CANCELLED: [],
    }

    def __init__(self):
        self._tasks: Dict[str, TaskLifecycle] = {}
        self._terminal_handlers: List[Callable] = []

    def register_terminal_handler(self, handler: Callable):
        self._terminal_handlers.append(handler)

    def create_task(self, task_id: str, payload: Dict, max_retries: int = 3) -> TaskLifecycle:
        task = TaskLifecycle(task_id=task_id, payload=payload, max_retries=max_retries)
        self._tasks[task_id] = task
        return task

    def transition(self, task_id: str, new_status: TaskStatus, result: Any = None, error: str = "") -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False

        valid = self.VALID_TRANSITIONS.get(task.status, [])
        if new_status not in valid:
            logger.warning(f"Invalid transition: {task.status} -> {new_status} for {task_id}")
            return False

        old_status = task.status
        task.status = new_status

        if new_status == TaskStatus.DISPATCHED:
            task.dispatched_at = time.time()
        elif new_status == TaskStatus.RUNNING:
            task.started_at = time.time()
        elif new_status == TaskStatus.COMPLETED:
            task.completed_at = time.time()
            task.result = result
            self._fire_terminal(task)
        elif new_status == TaskStatus.FAILED:
            task.completed_at = time.time()
            task.error = error
            task.error_class = classify_error(error)
            task.retry_count += 1
            if task.should_retry:
                task.status = TaskStatus.QUEUED
                task.started_at = 0
                task.completed_at = 0
                logger.info(f"Task {task_id} will retry ({task.retry_count}/{task.max_retries})")
            else:
                self._fire_terminal(task)
        elif new_status == TaskStatus.CANCELLED:
            task.completed_at = time.time()
            self._fire_terminal(task)

        logger.debug(f"Task {task_id}: {old_status} -> {task.status}")
        return True

    def _fire_terminal(self, task: TaskLifecycle):
        for handler in self._terminal_handlers:
            try:
                handler(task)
            except Exception as e:
                logger.error(f"Terminal handler error: {e}")

    def get_task(self, task_id: str) -> Optional[TaskLifecycle]:
        return self._tasks.get(task_id)

    def get_tasks_by_status(self, status: TaskStatus) -> List[TaskLifecycle]:
        return [t for t in self._tasks.values() if t.status == status]

    def get_tasks_by_agent(self, agent_name: str) -> List[TaskLifecycle]:
        return [t for t in self._tasks.values() if t.payload.get("agent") == agent_name]

    @property
    def stats(self) -> Dict:
        return {
            "total": len(self._tasks),
            "queued": len(self.get_tasks_by_status(TaskStatus.QUEUED)),
            "running": len(self.get_tasks_by_status(TaskStatus.RUNNING)),
            "completed": len(self.get_tasks_by_status(TaskStatus.COMPLETED)),
            "failed": len(self.get_tasks_by_status(TaskStatus.FAILED)),
            "cancelled": len(self.get_tasks_by_status(TaskStatus.CANCELLED)),
        }
