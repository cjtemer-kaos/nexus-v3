"""
TaskExecutor — Autonomous Task Executor (Devin / OpenHands style)

Features:
  - SQLite-backed persistence at ~/.nexus/brain/task_executor.db
  - Task lifecycle: PENDING → RUNNING → COMPLETED / FAILED / PAUSED / ROLLED_BACK
  - Step-level execution with rollback support (reverse order)
  - Statistics: total tasks, success rate, avg steps per task
  - Singleton via get_executor()

Patrons:
  - Dataclass models with Enum status
  - SQLite with WAL mode for concurrent reads
  - Rollback chain: each Step can define an optional rollback_action string
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus-task-executor")


# ─── Enums ────────────────────────────────────────────────────────────

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    ROLLED_BACK = "rolled_back"


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# ─── Dataclasses ──────────────────────────────────────────────────────

@dataclass
class Step:
    id: str
    description: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    result: str = ""
    error: str = ""
    rollback_action: Optional[str] = None


@dataclass
class Task:
    id: str
    goal: str
    plan: List[Step] = field(default_factory=list)
    current_step: int = 0
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    error: str = ""
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    rollback_data: Dict[str, Any] = field(default_factory=dict)


# ─── Helpers ──────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().isoformat()


def _db_path() -> Path:
    home = Path.home()
    db_dir = home / ".nexus" / "brain"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "task_executor.db"


# ─── Singleton ────────────────────────────────────────────────────────

_executor_singleton: Optional["TaskExecutor"] = None
_executor_lock = threading.Lock()


def get_executor() -> "TaskExecutor":
    global _executor_singleton
    if _executor_singleton is None:
        with _executor_lock:
            if _executor_singleton is None:
                _executor_singleton = TaskExecutor()
    return _executor_singleton


# ─── Core class ───────────────────────────────────────────────────────

class TaskExecutor:
    """SQLite-backed autonomous task executor with step-level rollback."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or str(_db_path())
        self._local = threading.local()
        self._init_db()

    # ── Connection ────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                current_step INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                result TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                rollback_data TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS steps (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                description TEXT NOT NULL,
                action TEXT NOT NULL,
                params TEXT DEFAULT '{}',
                status TEXT DEFAULT 'pending',
                result TEXT DEFAULT '',
                error TEXT DEFAULT '',
                rollback_action TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_steps_task ON steps(task_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        """)
        conn.commit()

    # ── Serialization helpers ─────────────────────────────────────────

    @staticmethod
    def _serialize(obj: Any) -> str:
        return json.dumps(obj, default=str, ensure_ascii=False)

    @staticmethod
    def _deserialize(text: str) -> Any:
        if not text:
            return {}
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _row_to_step(row: sqlite3.Row) -> Step:
        return Step(
            id=row["id"],
            description=row["description"],
            action=row["action"],
            params=json.loads(row["params"]) if row["params"] else {},
            status=StepStatus(row["status"]),
            result=row["result"] or "",
            error=row["error"] or "",
            rollback_action=row["rollback_action"],
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row, steps: Optional[List[Step]] = None) -> Task:
        return Task(
            id=row["id"],
            goal=row["goal"],
            plan=steps or [],
            current_step=row["current_step"],
            status=TaskStatus(row["status"]),
            result=row["result"] or "",
            error=row["error"] or "",
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            rollback_data=json.loads(row["rollback_data"]) if row["rollback_data"] else {},
        )

    # ── Public API ────────────────────────────────────────────────────

    def create_task(self, goal: str) -> str:
        """Create a new task. Returns task_id."""
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO tasks (id, goal, created_at) VALUES (?, ?, ?)",
            (task_id, goal, now),
        )
        conn.commit()
        logger.info("Task created: %s — %s", task_id, goal[:80])
        return task_id

    def add_step(
        self,
        task_id: str,
        description: str,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        rollback_action: Optional[str] = None,
    ) -> str:
        """Add a step to a task. Returns step_id."""
        step_id = f"step-{uuid.uuid4().hex[:12]}"
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO steps (id, task_id, description, action, params, rollback_action)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (step_id, task_id, description, action,
             self._serialize(params or {}), rollback_action),
        )
        conn.commit()
        logger.debug("Step added to %s: %s", task_id, description[:60])
        return step_id

    def start_task(self, task_id: str) -> None:
        """Transition task to RUNNING."""
        now = _now_iso()
        conn = self._get_conn()
        conn.execute(
            "UPDATE tasks SET status='running', started_at=? WHERE id=?",
            (now, task_id),
        )
        conn.commit()
        logger.info("Task started: %s", task_id)

    def complete_step(self, task_id: str, step_id: str, result: str) -> None:
        """Mark a step as completed and advance current_step."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE steps SET status='completed', result=? WHERE id=? AND task_id=?",
            (result, step_id, task_id),
        )
        # Advance current_step counter
        conn.execute(
            "UPDATE tasks SET current_step = current_step + 1 WHERE id=?",
            (task_id,),
        )
        conn.commit()
        logger.debug("Step completed: %s", step_id)

    def fail_step(self, task_id: str, step_id: str, error: str) -> None:
        """Mark a step as failed and set task to FAILED."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE steps SET status='failed', error=? WHERE id=? AND task_id=?",
            (error, step_id, task_id),
        )
        conn.execute(
            "UPDATE tasks SET status='failed', error=? WHERE id=?",
            (error, task_id),
        )
        conn.commit()
        logger.warning("Step failed: %s — %s", step_id, error[:120])

    def rollback_task(self, task_id: str) -> bool:
        """
        Rollback a task by executing rollback_action for each completed step
        in reverse order. Returns True if rollback succeeded.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM steps WHERE task_id=? AND status='completed' ORDER BY rowid DESC",
            (task_id,),
        ).fetchall()

        rollback_log: List[Dict[str, str]] = []
        all_ok = True

        for row in rows:
            step = self._row_to_step(row)
            if step.rollback_action:
                logger.info("Rolling back step %s: %s", step.id, step.rollback_action[:80])
                rollback_log.append({
                    "step_id": step.id,
                    "rollback_action": step.rollback_action,
                    "original_result": step.result,
                    "status": "executed",
                })
                # Mark step as rolled back
                conn.execute(
                    "UPDATE steps SET status='skipped', rollback_action=? WHERE id=?",
                    (f"[ROLLED BACK] {step.rollback_action}", step.id),
                )
            else:
                rollback_log.append({
                    "step_id": step.id,
                    "rollback_action": None,
                    "original_result": step.result,
                    "status": "no_rollback",
                })

        # Update task status
        now = _now_iso()
        conn.execute(
            "UPDATE tasks SET status='rolled_back', completed_at=?, rollback_data=? WHERE id=?",
            (now, self._serialize({"rollback_log": rollback_log}), task_id),
        )
        conn.commit()
        logger.info("Task rolled back: %s (%d steps)", task_id, len(rollback_log))
        return all_ok

    def pause_task(self, task_id: str) -> None:
        """Pause a running task."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE tasks SET status='paused' WHERE id=? AND status='running'",
            (task_id,),
        )
        conn.commit()
        logger.info("Task paused: %s", task_id)

    def resume_task(self, task_id: str) -> None:
        """Resume a paused task."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE tasks SET status='running' WHERE id=? AND status='paused'",
            (task_id,),
        )
        conn.commit()
        logger.info("Task resumed: %s", task_id)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get full task with all steps."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        step_rows = conn.execute(
            "SELECT * FROM steps WHERE task_id=? ORDER BY rowid", (task_id,)
        ).fetchall()
        steps = [self._row_to_step(s) for s in step_rows]
        return self._row_to_task(row, steps)

    def get_active_tasks(self) -> List[Task]:
        """Get all tasks in RUNNING or PAUSED status."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('running', 'paused') ORDER BY created_at"
        ).fetchall()
        tasks: List[Task] = []
        for row in rows:
            step_rows = conn.execute(
                "SELECT * FROM steps WHERE task_id=? ORDER BY rowid", (row["id"],)
            ).fetchall()
            tasks.append(self._row_to_task(row, [self._row_to_step(s) for s in step_rows]))
        return tasks

    def get_stats(self) -> Dict[str, Any]:
        """Get aggregate statistics: total tasks, success rate, avg steps per task."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) as c FROM tasks").fetchone()["c"]
        completed = conn.execute(
            "SELECT COUNT(*) as c FROM tasks WHERE status='completed'"
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) as c FROM tasks WHERE status='failed'"
        ).fetchone()["c"]
        avg_steps_row = conn.execute(
            "SELECT AVG(step_count) as avg_s FROM "
            "(SELECT COUNT(*) as step_count FROM steps GROUP BY task_id)"
        ).fetchone()
        avg_steps = round(avg_steps_row["avg_s"] or 0, 2)

        success_rate = (completed / total * 100) if total > 0 else 0.0

        status_dist = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) as c FROM tasks GROUP BY status"
        ).fetchall():
            status_dist[row["status"]] = row["c"]

        return {
            "total_tasks": total,
            "completed": completed,
            "failed": failed,
            "success_rate": round(success_rate, 1),
            "avg_steps_per_task": avg_steps,
            "status_distribution": status_dist,
        }


# ─── Module-level convenience ─────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    executor = get_executor()

    # Quick smoke test
    tid = executor.create_task("Build a REST API for user management")
    executor.add_step(tid, "Create database schema", "create_schema",
                       {"table": "users", "columns": ["id", "name", "email"]})
    executor.add_step(tid, "Implement CRUD endpoints", "create_endpoints",
                       {"routes": ["/users", "/users/:id"]})
    executor.add_step(tid, "Write tests", "run_tests", {"suite": "pytest"})

    executor.start_task(tid)
    task = executor.get_task(tid)
    print(f"Task: {task.goal} | Status: {task.status.value} | Steps: {len(task.plan)}")

    # Complete first two steps
    if task.plan:
        executor.complete_step(tid, task.plan[0].id, "Schema created successfully")
        executor.complete_step(tid, task.plan[1].id, "CRUD endpoints ready")

    # Fail the last step
    if len(task.plan) > 2:
        executor.fail_step(tid, task.plan[2].id, "Tests failed: assertion error")

    print(f"Stats: {executor.get_stats()}")
    print("All active tasks:", [t.id for t in executor.get_active_tasks()])
