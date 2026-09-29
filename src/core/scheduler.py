"""
Task Scheduler - SuperNEXUS v2
Cron, event triggers, task chaining, notificaciones multi-canal.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("NEXUS_DATA", Path.home() / ".nexus")) / "scheduler"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TASKS_FILE = DATA_DIR / "tasks.json"
RUNS_FILE = DATA_DIR / "runs.json"


class ScheduleType(str, Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    CRON = "cron"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    SKIPPED = "skipped"


class ScheduledTask:
    def __init__(self, data: Dict):
        self.id: str = data.get("id", "")
        self.name: str = data.get("name", "")
        self.prompt: str = data.get("prompt", "")
        self.schedule_type: str = data.get("schedule_type", "once")
        self.cron_expression: str = data.get("cron_expression", "")
        self.scheduled_time: str = data.get("scheduled_time", "")
        self.scheduled_day: int = data.get("scheduled_day", 0)
        self.enabled: bool = data.get("enabled", True)
        self.gema: str = data.get("gema", "auto")
        self.project: str = data.get("project", "default")
        self.notification: str = data.get("notification", "browser")
        self.then_task_id: Optional[str] = data.get("then_task_id")
        self.created_at: float = data.get("created_at", time.time())
        self.last_run: Optional[float] = data.get("last_run")
        self.next_run: Optional[float] = data.get("next_run")
        self.run_count: int = data.get("run_count", 0)
        self.metadata: Dict = data.get("metadata", {})

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "name": self.name, "prompt": self.prompt,
            "schedule_type": self.schedule_type, "cron_expression": self.cron_expression,
            "scheduled_time": self.scheduled_time, "scheduled_day": self.scheduled_day,
            "enabled": self.enabled, "gema": self.gema, "project": self.project,
            "notification": self.notification, "then_task_id": self.then_task_id,
            "created_at": self.created_at, "last_run": self.last_run,
            "next_run": self.next_run, "run_count": self.run_count,
            "metadata": self.metadata,
        }


class TaskRun:
    def __init__(self, data: Dict):
        self.id: str = data.get("id", "")
        self.task_id: str = data.get("task_id", "")
        self.status: str = data.get("status", "queued")
        self.started_at: float = data.get("started_at", 0)
        self.completed_at: Optional[float] = data.get("completed_at")
        self.output: str = data.get("output", "")
        self.error: Optional[str] = data.get("error")

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "task_id": self.task_id, "status": self.status,
            "started_at": self.started_at, "completed_at": self.completed_at,
            "output": self.output, "error": self.error,
        }


class TaskScheduler:
    """Scheduler principal con cron, event triggers y task chaining."""

    def __init__(self, llm_caller: Optional[Callable] = None):
        self.tasks: Dict[str, ScheduledTask] = {}
        self.runs: List[TaskRun] = []
        self._llm_caller = llm_caller
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._load()

    def _load(self):
        """Cargar tareas persistidas"""
        try:
            if TASKS_FILE.exists():
                data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
                for t in data.get("tasks", []):
                    task = ScheduledTask(t)
                    self.tasks[task.id] = task
        except Exception as e:
            logger.error(f"Error cargando tareas: {e}")

        try:
            if RUNS_FILE.exists():
                data = json.loads(RUNS_FILE.read_text(encoding="utf-8"))
                for r in data.get("runs", [])[-200:]:
                    self.runs.append(TaskRun(r))
        except Exception as e:
            logger.error(f"Error cargando runs: {e}")

    def _save(self):
        """Persistir tareas"""
        try:
            TASKS_FILE.write_text(json.dumps({
                "tasks": [t.to_dict() for t in self.tasks.values()]
            }, indent=2, ensure_ascii=False), encoding="utf-8")

            RUNS_FILE.write_text(json.dumps({
                "runs": [r.to_dict() for r in self.runs[-200:]]
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error guardando tareas: {e}")

    def add_task(self, task_data: Dict) -> ScheduledTask:
        """Crear tarea"""
        import uuid
        task = ScheduledTask({
            "id": str(uuid.uuid4())[:8],
            "created_at": time.time(),
            **task_data,
        })
        task.next_run = self._compute_next_run(task)
        self.tasks[task.id] = task
        self._save()
        logger.info(f"Tarea creada: {task.name} ({task.id})")
        return task

    def update_task(self, task_id: str, updates: Dict) -> Optional[ScheduledTask]:
        """Actualizar tarea"""
        task = self.tasks.get(task_id)
        if not task:
            return None
        for k, v in updates.items():
            if hasattr(task, k):
                setattr(task, k, v)
        task.next_run = self._compute_next_run(task)
        self._save()
        return task

    def delete_task(self, task_id: str) -> bool:
        """Eliminar tarea"""
        if task_id in self.tasks:
            del self.tasks[task_id]
            self._save()
            return True
        return False

    def list_tasks(self, enabled_only: bool = False) -> List[Dict]:
        """Listar tareas"""
        tasks = list(self.tasks.values())
        if enabled_only:
            tasks = [t for t in tasks if t.enabled]
        return [t.to_dict() for t in tasks]

    def list_runs(self, task_id: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """Listar ejecuciones"""
        runs = self.runs
        if task_id:
            runs = [r for r in runs if r.task_id == task_id]
        return [r.to_dict() for r in runs[-limit:]]

    def _compute_next_run(self, task: ScheduledTask) -> Optional[float]:
        """Calcular proxima ejecucion"""
        now = time.time()
        if not task.enabled:
            return None

        if task.schedule_type == "once":
            if task.scheduled_time:
                try:
                    dt = datetime.fromisoformat(task.scheduled_time)
                    ts = dt.timestamp()
                    return ts if ts > now else None
                except Exception:
                    pass

        elif task.schedule_type == "daily":
            if task.scheduled_time:
                try:
                    h, m = map(int, task.scheduled_time.split(":"))
                    today = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                    if today.timestamp() <= now:
                        today += timedelta(days=1)
                    return today.timestamp()
                except Exception:
                    pass

        elif task.schedule_type == "weekly":
            if task.scheduled_time and task.scheduled_day >= 0:
                try:
                    h, m = map(int, task.scheduled_time.split(":"))
                    now_dt = datetime.now()
                    days_ahead = task.scheduled_day - now_dt.weekday()
                    if days_ahead <= 0:
                        days_ahead += 7
                    target = now_dt.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_ahead)
                    return target.timestamp()
                except Exception:
                    pass

        elif task.schedule_type == "cron" and task.cron_expression:
            try:
                from croniter import croniter
                cron = croniter(task.cronic_expression, datetime.now())
                return cron.get_next(float)
            except ImportError:
                logger.warning("croniter no instalado")
            except Exception:
                pass

        return now + 3600

    async def start(self):
        """Iniciar loop del scheduler"""
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._loop())
        logger.info("Scheduler iniciado")

    async def stop(self):
        """Detener scheduler"""
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler detenido")

    async def _loop(self):
        """Loop principal — verifica tareas cada 30s"""
        while self._running:
            try:
                await self._check_due_tasks()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
                await asyncio.sleep(60)

    async def _check_due_tasks(self):
        """Verificar tareas pendientes"""
        now = time.time()
        for task in list(self.tasks.values()):
            if not task.enabled or not task.next_run:
                continue
            if task.next_run <= now:
                asyncio.create_task(self._execute_task(task.id))

    async def _execute_task(self, task_id: str):
        """Ejecutar una tarea"""
        task = self.tasks.get(task_id)
        if not task:
            return

        run_id = f"run-{int(time.time())}-{task_id}"
        run = TaskRun({
            "id": run_id, "task_id": task_id,
            "status": "running", "started_at": time.time(),
        })
        self.runs.append(run)

        try:
            output = ""
            if self._llm_caller:
                output = await self._llm_caller(task.prompt, task.gema)
            else:
                output = f"[Scheduler] Tarea '{task.name}' ejecutada (sin LLM configurado)"

            run.status = "success"
            run.output = output[:5000]
            run.completed_at = time.time()

            task.last_run = time.time()
            task.run_count += 1
            task.next_run = self._compute_next_run(task)

            self._save()

            if task.then_task_id and task.then_task_id in self.tasks:
                asyncio.create_task(self._execute_task(task.then_task_id))

        except Exception as e:
            run.status = "error"
            run.error = str(e)[:2000]
            run.completed_at = time.time()
            logger.error(f"Task error: {e}")

    async def fire_event(self, event_type: str, data: Dict = None):
        """Disparar evento — ejecuta tareas con trigger de tipo evento"""
        for task in self.tasks.values():
            if task.enabled and task.metadata.get("trigger") == "event":
                if event_type in task.metadata.get("events", []):
                    asyncio.create_task(self._execute_task(task.id))

    def get_status(self) -> Dict:
        """Estado del scheduler"""
        enabled = sum(1 for t in self.tasks.values() if t.enabled)
        return {
            "running": self._running,
            "total_tasks": len(self.tasks),
            "enabled_tasks": enabled,
            "total_runs": len(self.runs),
            "recent_runs": [r.to_dict() for r in self.runs[-5:]],
        }
