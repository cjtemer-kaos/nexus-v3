"""
Task Heartbeat — Task liveliness monitoring.

Pattern extracted from openclaw task-heartbeat.ts.
Detects stale tasks, abandoned agents, and generates alerts when tasks
exceed their expected duration without a heartbeat.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Callable

logger = logging.getLogger("nexus-heartbeat")


class HeartbeatStatus(str, Enum):
    ALIVE = "alive"
    STALE = "stale"
    ZOMBIE = "zombie"
    COMPLETED = "completed"


@dataclass
class TaskHeartbeatRecord:
    task_id: str = ""
    agent_id: str = ""
    status: HeartbeatStatus = HeartbeatStatus.ALIVE
    last_beat: float = 0.0
    started_at: float = 0.0
    expected_duration: float = 300.0
    stale_threshold: float = 60.0
    zombie_threshold: float = 600.0
    beat_count: int = 0
    message: str = ""

    def __post_init__(self):
        now = time.time()
        if not self.last_beat:
            self.last_beat = now
        if not self.started_at:
            self.started_at = now

    @property
    def age(self) -> float:
        return time.time() - self.started_at

    @property
    def idle(self) -> float:
        return time.time() - self.last_beat

    @property
    def is_stale(self) -> bool:
        return self.idle > self.stale_threshold and self.status != HeartbeatStatus.COMPLETED

    @property
    def is_zombie(self) -> bool:
        return self.idle > self.zombie_threshold and self.status != HeartbeatStatus.COMPLETED

    @property
    def is_overdue(self) -> bool:
        return self.age > self.expected_duration and self.status != HeartbeatStatus.COMPLETED


class TaskHeartbeat:
    def __init__(self):
        self._tasks: Dict[str, TaskHeartbeatRecord] = {}
        self._callbacks: Dict[str, List[Callable]] = {
            "stale": [],
            "zombie": [],
            "overdue": [],
        }

    def register(self, task_id: str, agent_id: str, expected_duration: float = 300.0,
                 stale_threshold: float = 60.0, zombie_threshold: float = 600.0) -> TaskHeartbeatRecord:
        record = TaskHeartbeatRecord(
            task_id=task_id,
            agent_id=agent_id,
            expected_duration=expected_duration,
            stale_threshold=stale_threshold,
            zombie_threshold=zombie_threshold,
        )
        self._tasks[task_id] = record
        logger.info(f"Heartbeat registered: {task_id} (agent: {agent_id}, expected: {expected_duration}s)")
        return record

    def beat(self, task_id: str, message: str = "") -> Optional[TaskHeartbeatRecord]:
        record = self._tasks.get(task_id)
        if not record:
            return None
        record.last_beat = time.time()
        record.beat_count += 1
        if message:
            record.message = message
        if record.status in (HeartbeatStatus.STALE, HeartbeatStatus.ZOMBIE):
            record.status = HeartbeatStatus.ALIVE
        return record

    def complete(self, task_id: str) -> Optional[TaskHeartbeatRecord]:
        record = self._tasks.get(task_id)
        if not record:
            return None
        record.status = HeartbeatStatus.COMPLETED
        logger.info(f"Heartbeat completed: {task_id}")
        return record

    def get_status(self, task_id: str) -> Optional[Dict]:
        record = self._tasks.get(task_id)
        if not record:
            return None
        return {
            "task_id": record.task_id,
            "agent_id": record.agent_id,
            "status": record.status.value,
            "age_seconds": round(record.age, 1),
            "idle_seconds": round(record.idle, 1),
            "beat_count": record.beat_count,
            "expected_duration": record.expected_duration,
            "is_stale": record.is_stale,
            "is_zombie": record.is_zombie,
            "is_overdue": record.is_overdue,
            "message": record.message,
        }

    def check_all(self) -> Dict[str, List[str]]:
        alerts = {"stale": [], "zombie": [], "overdue": []}
        for task_id, record in self._tasks.items():
            if record.status == HeartbeatStatus.COMPLETED:
                continue
            previous = record.status
            if record.is_zombie:
                record.status = HeartbeatStatus.ZOMBIE
                if previous != HeartbeatStatus.ZOMBIE:
                    alerts["zombie"].append(task_id)
                    logger.warning(f"ZOMBIE task detected: {task_id} (idle: {record.idle:.0f}s)")
                    for cb in self._callbacks["zombie"]:
                        cb(task_id, record)
            elif record.is_stale:
                record.status = HeartbeatStatus.STALE
                if previous != HeartbeatStatus.STALE:
                    alerts["stale"].append(task_id)
                    logger.warning(f"STALE task detected: {task_id} (idle: {record.idle:.0f}s)")
                    for cb in self._callbacks["stale"]:
                        cb(task_id, record)
            if record.is_overdue and task_id not in alerts["zombie"]:
                alerts["overdue"].append(task_id)
                for cb in self._callbacks["overdue"]:
                    cb(task_id, record)
        return alerts

    def on_stale(self, callback: Callable):
        self._callbacks["stale"].append(callback)

    def on_zombie(self, callback: Callable):
        self._callbacks["zombie"].append(callback)

    def on_overdue(self, callback: Callable):
        self._callbacks["overdue"].append(callback)

    def get_all_statuses(self) -> Dict[str, Dict]:
        return {
            task_id: self.get_status(task_id)
            for task_id in self._tasks
        }

    def get_stats(self) -> Dict:
        total = len(self._tasks)
        alive = sum(1 for t in self._tasks.values() if t.status == HeartbeatStatus.ALIVE)
        stale = sum(1 for t in self._tasks.values() if t.status == HeartbeatStatus.STALE)
        zombie = sum(1 for t in self._tasks.values() if t.status == HeartbeatStatus.ZOMBIE)
        completed = sum(1 for t in self._tasks.values() if t.status == HeartbeatStatus.COMPLETED)
        return {
            "total": total,
            "alive": alive,
            "stale": stale,
            "zombie": zombie,
            "completed": completed,
        }

    def cleanup_completed(self):
        self._tasks = {
            tid: t for tid, t in self._tasks.items()
            if t.status != HeartbeatStatus.COMPLETED
        }
