"""
Task Queue - Cola de tareas con ordenamiento topologico y dependencias adaptativas

- Topological sort para determinar orden de ejecucion
- Dependencias adaptativas basadas en resultados previos
- Prioridad dinamica segun complejidad de tarea
- Soporte para ejecucion paralela de tareas independientes

Patrones:
- Kahn's algorithm para topological sort
- Dependency resolution con deteccion de ciclos
- Dynamic priority adjustment
- Task chaining con resultado como input
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from src.core.error_envelope import ErrorEnvelope, ErrorCategory, ErrorSeverity, ErrorDecision

logger = logging.getLogger("nexus-task-queue")


class TaskPriority(Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


class TaskStatus(Enum):
    QUEUED = "queued"
    WAITING = "waiting"  # Waiting for dependencies
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


@dataclass
class TaskDependency:
    """Dependencia entre tareas"""
    task_id: str
    condition: str = "completed"  # completed, failed, any
    output_key: str = ""  # Key del resultado a usar como input


@dataclass
class QueuedTask:
    """Tarea en la cola"""
    id: str
    name: str
    description: str = ""
    priority: TaskPriority = TaskPriority.NORMAL
    dependencies: List[TaskDependency] = field(default_factory=list)
    executor: Optional[Callable] = None
    params: Dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.QUEUED
    result: Any = None
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: str = ""
    completed_at: str = ""
    retries: int = 0
    max_retries: int = 2
    timeout_seconds: float = 300
    tags: List[str] = field(default_factory=list)

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        if isinstance(other, QueuedTask):
            return self.id == other.id
        return False


class DependencyGraph:
    """
    Grafo de dependencias con deteccion de ciclos y topological sort.
    Implementa Kahn's algorithm.
    """

    def __init__(self):
        self._edges: Dict[str, Set[str]] = defaultdict(set)  # task -> dependencies
        self._dependents: Dict[str, Set[str]] = defaultdict(set)  # task -> tasks that depend on it

    def add_task(self, task_id: str, dependencies: List[str] = None):
        """Agrega tarea con sus dependencias"""
        if task_id not in self._edges:
            self._edges[task_id] = set()
        if dependencies:
            for dep in dependencies:
                self._edges[task_id].add(dep)
                self._dependents[dep].add(task_id)

    def remove_task(self, task_id: str):
        """Elimina tarea del grafo"""
        # Eliminar dependencias de esta tarea
        self._edges.pop(task_id, None)
        # Eliminar referencias en otras tareas
        for deps in self._edges.values():
            deps.discard(task_id)
        self._dependents.pop(task_id, None)
        for dependents in self._dependents.values():
            dependents.discard(task_id)

    def has_cycle(self) -> bool:
        """Detecta ciclos usando DFS"""
        visited = set()
        rec_stack = set()

        def dfs(node):
            visited.add(node)
            rec_stack.add(node)
            for dep in self._edges.get(node, set()):
                if dep not in visited:
                    if dfs(dep):
                        return True
                elif dep in rec_stack:
                    return True
            rec_stack.discard(node)
            return False

        for node in self._edges:
            if node not in visited:
                if dfs(node):
                    return True
        return False

    def topological_sort(self) -> List[str]:
        """
        Ordenamiento topologico usando Kahn's algorithm.
        Retorna lista de task_ids en orden de ejecucion.
        """
        # Calcular grado de entrada
        in_degree = defaultdict(int)
        for task_id in self._edges:
            if task_id not in in_degree:
                in_degree[task_id] = 0
            for dep in self._edges[task_id]:
                in_degree[dep] = in_degree.get(dep, 0)
                in_degree[task_id] += 1

        # Cola con tareas sin dependencias
        queue = deque([t for t in self._edges if in_degree[t] == 0])
        result = []

        while queue:
            # Ordenar por prioridad si hay multiples tareas listas
            queue = deque(sorted(queue))
            node = queue.popleft()
            result.append(node)

            for dependent in self._dependents.get(node, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(self._edges):
            # Hay ciclo
            remaining = set(self._edges.keys()) - set(result)
            logger.warning(f"Dependency cycle detected involving: {remaining}")
            return list(result)  # Retorna lo que se pudo ordenar

        return result

    def get_ready_tasks(self, completed: Set[str]) -> List[str]:
        """Obtiene tareas listas para ejecutar (todas las dependencias completadas)"""
        ready = []
        for task_id, deps in self._edges.items():
            if task_id in completed:
                continue
            if deps.issubset(completed):
                ready.append(task_id)
        return ready

    def get_execution_levels(self) -> List[List[str]]:
        """
        Retorna niveles de ejecucion paralela.
        Cada nivel contiene tareas que pueden ejecutarse en paralelo.
        """
        in_degree = defaultdict(int)
        for task_id in self._edges:
            if task_id not in in_degree:
                in_degree[task_id] = 0
            for dep in self._edges[task_id]:
                in_degree[task_id] += 1

        levels = []
        remaining = set(self._edges.keys())

        while remaining:
            # Tareas con todas las dependencias satisfechas
            level = [t for t in remaining if in_degree[t] == 0]
            if not level:
                # Ciclo detectado
                logger.warning(f"Cannot resolve dependencies, remaining: {remaining}")
                break

            levels.append(level)

            # Actualizar grados
            for task_id in level:
                remaining.discard(task_id)
                for dependent in self._dependents.get(task_id, set()):
                    if dependent in remaining:
                        in_degree[dependent] -= 1

        return levels


class TaskQueue:
    """
    Cola de tareas con resolucion de dependencias y ejecucion adaptativa.

    Uso:
        queue = TaskQueue()
        queue.add_task("task1", executor=func1, priority=TaskPriority.HIGH)
        queue.add_task("task2", executor=func2, dependencies=["task1"])
        results = await queue.execute_all()
    """

    def __init__(self, max_parallel: int = 5):
        self._tasks: Dict[str, QueuedTask] = {}
        self._graph = DependencyGraph()
        self._completed: Set[str] = set()
        self._failed: Set[str] = set()
        self._max_parallel = max_parallel
        self._results: Dict[str, Any] = {}
        self._callbacks: Dict[str, List[Callable]] = defaultdict(list)
        self._running = False

    def add_task(
        self,
        task_id: str,
        name: str,
        executor: Callable = None,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        dependencies: List[str] = None,
        params: Dict[str, Any] = None,
        max_retries: int = 2,
        timeout_seconds: float = 300,
        tags: List[str] = None,
    ) -> QueuedTask:
        """Agrega una tarea a la cola"""
        if task_id in self._tasks:
            raise ValueError(f"Task '{task_id}' already exists")

        task = QueuedTask(
            id=task_id,
            name=name,
            description=description,
            priority=priority,
            dependencies=[TaskDependency(dep) for dep in (dependencies or [])],
            executor=executor,
            params=params or {},
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            tags=tags or [],
        )

        self._tasks[task_id] = task
        self._graph.add_task(task_id, dependencies)

        # Detectar ciclos
        if self._graph.has_cycle():
            logger.warning(f"Adding task '{task_id}' created a dependency cycle")
            task.status = TaskStatus.FAILED
            task.error = "Dependency cycle detected"
            self._failed.add(task_id)

        logger.info(f"Task added: {task_id} (priority: {priority.name}, deps: {dependencies})")
        return task

    def remove_task(self, task_id: str) -> bool:
        """Elimina una tarea de la cola"""
        if task_id not in self._tasks:
            return False

        task = self._tasks[task_id]
        if task.status == TaskStatus.RUNNING:
            return False  # No se puede eliminar una tarea en ejecucion

        del self._tasks[task_id]
        self._graph.remove_task(task_id)
        self._completed.discard(task_id)
        self._failed.discard(task_id)
        return True

    def get_task(self, task_id: str) -> Optional[QueuedTask]:
        return self._tasks.get(task_id)

    def get_status(self) -> Dict:
        """Estado de la cola"""
        by_status = defaultdict(int)
        for task in self._tasks.values():
            by_status[task.status.value] += 1

        return {
            "total_tasks": len(self._tasks),
            "by_status": dict(by_status),
            "completed": len(self._completed),
            "failed": len(self._failed),
            "running": self._running,
            "execution_levels": self._graph.get_execution_levels(),
        }

    def on_complete(self, task_id: str, callback: Callable):
        """Registra callback para cuando una tarea se complete"""
        self._callbacks[task_id].append(callback)

    async def execute_task(self, task_id: str) -> Any:
        """Ejecuta una tarea individual"""
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")

        if task.status == TaskStatus.COMPLETED:
            return task.result

        # Verificar dependencias
        for dep in task.dependencies:
            if dep.task_id not in self._completed:
                if dep.task_id in self._failed and dep.condition == "completed":
                    task.status = TaskStatus.FAILED
                    task.error = f"Dependency '{dep.task_id}' failed"
                    self._failed.add(task_id)
                    return None
                # Esperar a que la dependencia se complete
                await self._wait_for_dependency(dep.task_id)

        # Ejecutar tarea
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now().isoformat()

        try:
            if task.executor:
                # Inyectar resultados de dependencias como params
                for dep in task.dependencies:
                    if dep.task_id in self._results:
                        if dep.output_key:
                            task.params[dep.output_key] = self._results[dep.task_id]
                        else:
                            task.params[f"dep_{dep.task_id}"] = self._results[dep.task_id]

                result = task.executor(**task.params)
                if asyncio.iscoroutine(result):
                    result = await asyncio.wait_for(result, timeout=task.timeout_seconds)

                task.result = result
                task.status = TaskStatus.COMPLETED
                task.completed_at = datetime.now().isoformat()
                self._completed.add(task_id)
                self._results[task_id] = result

                # Ejecutar callbacks
                for callback in self._callbacks.get(task_id, []):
                    try:
                        cb_result = callback(task_id, result)
                        if asyncio.iscoroutine(cb_result):
                            await cb_result
                    except Exception as e:
                        logger.error(f"Callback error for task {task_id}: {e}")

                logger.info(f"Task completed: {task_id}")
                return result
            else:
                task.status = TaskStatus.FAILED
                task.error = "No executor defined"
                self._failed.add(task_id)
                return None

        except asyncio.TimeoutError:
            task.error = f"Timeout after {task.timeout_seconds}s"
            logger.error(f"Task timeout: {task_id}")
            # ErrorEnvelope: Clasificar error de timeout
            envelope = ErrorEnvelope(
                error=asyncio.TimeoutError(),
                category=ErrorCategory.RESOURCE,
                severity=ErrorSeverity.MEDIUM,
                decision=ErrorDecision.RETRY,
                max_retries=task.max_retries,
                retry_count=task.retries,
                message=f"Timeout after {task.timeout_seconds}s",
                fix_suggestion="Increase timeout_seconds or optimize task executor",
            )
            task.error = envelope.message
        except Exception as e:
            # ErrorEnvelope: Clasificar error para decision inteligente
            envelope = ErrorEnvelope(
                error=e,
                category=ErrorCategory.UNKNOWN,
                severity=ErrorSeverity.MEDIUM,
                decision=ErrorDecision.RETRY,
                max_retries=task.max_retries,
                retry_count=task.retries,
                message=str(e),
                fix_suggestion="Check task executor and parameters",
            )
            envelope.classify()  # Auto-clasificar segun tipo de excepcion
            task.error = envelope.message
            logger.error(f"Task error: {task_id} - {envelope.category.value} ({envelope.severity.value})")
            
            # Decision basada en ErrorEnvelope
            if envelope.decision == ErrorDecision.ABORT:
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now().isoformat()
                self._failed.add(task_id)
                logger.critical(f"Task aborted: {task_id} - {envelope.message}")
                return None
            elif envelope.decision == ErrorDecision.ESCALATE:
                logger.warning(f"Task requires escalation: {task_id}")
                # No reintentar, marcar como fallido
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now().isoformat()
                self._failed.add(task_id)
                return None

        # Reintentar si es posible
        if task.retries < task.max_retries:
            task.retries += 1
            task.status = TaskStatus.RETRYING
            logger.info(f"Retrying task: {task_id} (attempt {task.retries}/{task.max_retries})")
            await asyncio.sleep(1)  # Breve pausa antes de reintentar
            return await self.execute_task(task_id)

        task.status = TaskStatus.FAILED
        task.completed_at = datetime.now().isoformat()
        self._failed.add(task_id)
        return None

    async def _wait_for_dependency(self, dep_task_id: str, timeout: float = 600):
        """Espera a que una dependencia se complete"""
        start = time.time()
        while dep_task_id not in self._completed and dep_task_id not in self._failed:
            if time.time() - start > timeout:
                raise TimeoutError(f"Dependency '{dep_task_id}' timed out")
            await asyncio.sleep(0.1)

        if dep_task_id in self._failed:
            raise RuntimeError(f"Dependency '{dep_task_id}' failed")

    async def execute_all(self) -> Dict[str, Any]:
        """
        Ejecuta todas las tareas respetando dependencias.
        Tareas independientes se ejecutan en paralelo.
        """
        self._running = True
        levels = self._graph.get_execution_levels()

        for level in levels:
            # Filtrar tareas que ya estan completadas o fallidas
            pending = [t for t in level if t in self._tasks and self._tasks[t].status == TaskStatus.QUEUED]

            if not pending:
                continue

            # Ejecutar tareas del nivel en paralelo (limitado por max_parallel)
            semaphore = asyncio.Semaphore(self._max_parallel)

            async def run_with_semaphore(task_id):
                async with semaphore:
                    return await self.execute_task(task_id)

            tasks = [run_with_semaphore(tid) for tid in pending]
            await asyncio.gather(*tasks, return_exceptions=True)

        self._running = False

        # Recopilar resultados
        return {
            task_id: task.result
            for task_id, task in self._tasks.items()
            if task.status == TaskStatus.COMPLETED
        }

    async def execute_parallel(self, task_ids: List[str]) -> Dict[str, Any]:
        """Ejecuta un grupo de tareas en paralelo"""
        semaphore = asyncio.Semaphore(self._max_parallel)

        async def run_with_semaphore(task_id):
            async with semaphore:
                return task_id, await self.execute_task(task_id)

        results = await asyncio.gather(
            *[run_with_semaphore(tid) for tid in task_ids],
            return_exceptions=True,
        )

        return {tid: result for tid, result in results if not isinstance(result, Exception)}

    def clear(self):
        """Limpia la cola"""
        self._tasks.clear()
        self._graph = DependencyGraph()
        self._completed.clear()
        self._failed.clear()
        self._results.clear()
        self._callbacks.clear()
        self._running = False

    def get_execution_order(self) -> List[str]:
        """Retorna el orden de ejecucion sin ejecutar"""
        return self._graph.topological_sort()

    def get_parallel_groups(self) -> List[List[str]]:
        """Retorna grupos de tareas que pueden ejecutarse en paralelo"""
        return self._graph.get_execution_levels()
