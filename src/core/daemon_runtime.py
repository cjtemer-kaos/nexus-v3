"""
Daemon Runtime - Background agent supervisor with auto-recovery.
Absorbed from multica pattern — names cleaned.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentRuntime:
    runtime_id: str
    agent_name: str
    status: str = "idle"
    last_heartbeat: float = 0.0
    task_count: int = 0
    error_count: int = 0


@dataclass
class DaemonTask:
    task_id: str
    agent_name: str
    status: str = "queued"
    payload: Dict = field(default_factory=dict)
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    result: Any = None
    error: str = ""
    retry_count: int = 0
    max_retries: int = 3


class DaemonManager:
    """Supervisor for background agent runtimes with auto-recovery."""

    def __init__(self):
        self._runtimes: Dict[str, AgentRuntime] = {}
        self._task_queue: asyncio.Queue = asyncio.Queue()
        self._running_tasks: Dict[str, DaemonTask] = {}
        self._handlers: Dict[str, Callable] = {}
        self._running = False
        self._heartbeat_interval = 15
        self._gc_interval = 60
        self._idle_timeout = 300

    def register_handler(self, agent_name: str, handler: Callable):
        self._handlers[agent_name] = handler

    async def start(self):
        if self._running:
            return
        self._running = True
        logger.info("Daemon manager started")
        await asyncio.gather(
            self._heartbeat_loop(),
            self._task_dispatch_loop(),
            self._gc_loop(),
        )

    async def stop(self):
        self._running = False
        logger.info("Daemon manager stopped")

    async def submit_task(self, agent_name: str, payload: Dict, max_retries: int = 3) -> str:
        task_id = f"task_{int(time.time() * 1000)}"
        task = DaemonTask(
            task_id=task_id,
            agent_name=agent_name,
            payload=payload,
            created_at=time.time(),
            max_retries=max_retries,
        )
        await self._task_queue.put(task)
        return task_id

    async def get_task_status(self, task_id: str) -> Optional[DaemonTask]:
        return self._running_tasks.get(task_id)

    async def _task_dispatch_loop(self):
        while self._running:
            try:
                task = await asyncio.wait_for(self._task_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            handler = self._handlers.get(task.agent_name)
            if not handler:
                task.status = "failed"
                task.error = f"No handler for agent: {task.agent_name}"
                continue

            task.status = "running"
            task.started_at = time.time()
            self._running_tasks[task.task_id] = task

            try:
                if asyncio.iscoroutinefunction(handler):
                    task.result = await handler(task.payload)
                else:
                    task.result = handler(task.payload)
                task.status = "completed"
                task.completed_at = time.time()
                logger.info(f"Task {task.task_id} completed")
            except Exception as e:
                task.error = str(e)
                task.retry_count += 1
                if task.retry_count < task.max_retries:
                    task.status = "queued"
                    await self._task_queue.put(task)
                    logger.warning(f"Task {task.task_id} retry {task.retry_count}/{task.max_retries}")
                else:
                    task.status = "failed"
                    task.completed_at = time.time()
                    logger.error(f"Task {task.task_id} failed permanently: {e}")

    async def _heartbeat_loop(self):
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            for rid, runtime in self._runtimes.items():
                runtime.last_heartbeat = time.time()
            stale = [rid for rid, r in self._runtimes.items() if time.time() - r.last_heartbeat > self._idle_timeout * 2]
            for rid in stale:
                logger.warning(f"Runtime {rid} stale, removing")
                del self._runtimes[rid]

    async def _gc_loop(self):
        while self._running:
            await asyncio.sleep(self._gc_interval)
            cutoff = time.time() - 3600
            expired = [tid for tid, t in self._running_tasks.items() if t.completed_at > 0 and t.completed_at < cutoff]
            for tid in expired:
                del self._running_tasks[tid]
            if expired:
                logger.info(f"GC: cleaned {len(expired)} completed tasks")

    @property
    def stats(self) -> Dict:
        return {
            "runtimes": len(self._runtimes),
            "queued": self._task_queue.qsize(),
            "running": sum(1 for t in self._running_tasks.values() if t.status == "running"),
            "completed": sum(1 for t in self._running_tasks.values() if t.status == "completed"),
            "failed": sum(1 for t in self._running_tasks.values() if t.status == "failed"),
        }
