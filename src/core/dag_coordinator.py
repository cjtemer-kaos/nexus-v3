"""
F2: Goal-to-DAG Decomposition

Coordinator agent breaks natural language goals into structured task graphs (DAGs).
Supports dependency resolution, parallel execution, and result synthesis.
"""

import asyncio
import json
import logging
import os
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from src.core.task_queue import TaskQueue, TaskPriority
from src.core.error_envelope import ErrorClassifier, ErrorDecision

logger = logging.getLogger("nexus-dag")

# LLM endpoint for decomposition
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_DECOMPOSE_MODEL", "nexus-director-v6")


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskNode:
    id: str
    title: str
    description: str
    assignee: str = "auto"
    depends_on: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    error: str = ""
    started_at: str = ""
    completed_at: str = ""
    retries: int = 0
    max_retries: int = 2


@dataclass
class TaskDAG:
    id: str
    goal: str
    nodes: List[TaskNode] = field(default_factory=list)
    status: str = "pending"
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    max_parallel: int = 3

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def get_ready_tasks(self) -> List[TaskNode]:
        """Get tasks whose dependencies are all completed"""
        completed_ids = {n.id for n in self.nodes if n.status == TaskStatus.COMPLETED}
        ready = []
        for node in self.nodes:
            if node.status != TaskStatus.PENDING:
                continue
            if all(dep in completed_ids for dep in node.depends_on):
                ready.append(node)
        return ready

    def is_complete(self) -> bool:
        return all(n.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED) for n in self.nodes)

    def get_completion_percent(self) -> float:
        if not self.nodes:
            return 100.0
        done = sum(1 for n in self.nodes if n.status == TaskStatus.COMPLETED)
        return round((done / len(self.nodes)) * 100, 1)

    def get_failed_tasks(self) -> List[TaskNode]:
        return [n for n in self.nodes if n.status == TaskStatus.FAILED]


class DAGCoordinator:
    """Decomposes goals into task DAGs and orchestrates execution"""

    def __init__(self, available_agents: Optional[List[str]] = None):
        self.available_agents = available_agents or [
            "director", "code", "scholar", "architect", "creative",
            "analyst", "debugger", "optimizer", "tester", "devops",
        ]
        self._runs: Dict[str, TaskDAG] = {}

    def decompose_goal(self, goal: str, run_id: str = None, use_llm: bool = True) -> TaskDAG:
        """
        Decompose a goal into a task DAG.
        Uses LLM-based decomposition when goal is complex (>15 words or multiple actions).
        Falls back to pattern-based for simple goals.
        """
        import uuid
        rid = run_id or str(uuid.uuid4())[:8]

        # Decide: LLM or pattern-based
        goal_words = len(goal.split())
        is_complex = goal_words > 15 or sum(1 for kw in ["y ", "and ", "luego ", "después ", "también "] if kw in goal.lower()) >= 2

        if use_llm and is_complex:
            tasks = asyncio.get_event_loop().run_until_complete(
                self._decompose_with_llm(goal)
            )
        else:
            tasks = self._parse_goal(goal)

        dag = TaskDAG(id=rid, goal=goal, nodes=tasks)
        self._runs[rid] = dag
        logger.info(f"DAG created: {rid} with {len(tasks)} tasks for goal: {goal[:80]} (method: {'llm' if is_complex and use_llm else 'pattern'})")
        return dag

    async def _decompose_with_llm(self, goal: str) -> List[TaskNode]:
        """Use Ollama LLM to decompose a complex goal into task nodes.

        Sends a structured prompt asking the LLM to return JSON with tasks,
        dependencies, and assignees. Falls back to pattern-based on failure.
        """
        prompt = f"""Eres un coordinador de tareas. Descompón este objetivo en un grafo de tareas (DAG).

Objetivo: {goal}

Responde SOLO con JSON válido (sin markdown, sin explicaciones):
{{
  "tasks": [
    {{
      "id": "t01",
      "title": "Título corto",
      "description": "Descripción detallada de la tarea",
      "assignee": "director|code|scholar|architect|analyst|debugger|optimizer|tester|devops|creative",
      "depends_on": []
    }}
  ]
}}

Reglas:
- Cada task tiene un id único (t01, t02, ...)
- depends_on usa los ids de las tareas de las que depende
- Si dos tareas pueden correr en paralelo, pon las mismas dependencias
- Máximo 8 tareas
- assignee debe ser uno de los valores válidos
- Si no puedes descomponer, retorna 1 sola tarea"""

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={
                        "model": OLLAMA_MODEL,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.3, "num_predict": 1000},
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    raw = data.get("response", "")
                    # Extract JSON from response (may have markdown wrapper)
                    json_start = raw.find("{")
                    json_end = raw.rfind("}") + 1
                    if json_start >= 0 and json_end > json_start:
                        parsed = json.loads(raw[json_start:json_end])
                        tasks_data = parsed.get("tasks", [])
                        if tasks_data:
                            return self._llm_to_nodes(tasks_data, goal)
        except Exception as e:
            logger.warning(f"LLM decomposition failed, falling back to pattern: {e}")

        # Fallback to pattern-based
        return self._parse_goal(goal)

    def _llm_to_nodes(self, tasks_data: list, goal: str) -> List[TaskNode]:
        """Convert LLM JSON output to TaskNode objects"""
        nodes = []
        valid_assignees = {"director", "code", "scholar", "architect", "analyst",
                           "debugger", "optimizer", "tester", "devops", "creative"}
        for td in tasks_data:
            assignee = td.get("assignee", "director")
            if assignee not in valid_assignees:
                assignee = "director"
            nodes.append(TaskNode(
                id=td.get("id", f"t{len(nodes)+1:02d}"),
                title=td.get("title", "Task"),
                description=td.get("description", goal),
                assignee=assignee,
                depends_on=td.get("depends_on", []),
            ))
        return nodes if nodes else self._parse_goal(goal)

    def _parse_goal(self, goal: str) -> List[TaskNode]:
        """Parse goal into task nodes using pattern matching"""
        goal_lower = goal.lower()
        tasks = []

        # Multi-step patterns
        if any(kw in goal_lower for kw in ["crear", "create", "desarrollar", "develop", "construir", "build"]):
            tasks.extend([
                TaskNode(id="plan", title="Planificar", description=f"Analizar requisitos y crear plan de acción para: {goal}", assignee="architect"),
                TaskNode(id="design", title="Diseñar", description="Diseñar arquitectura y estructura del sistema", assignee="architect", depends_on=["plan"]),
                TaskNode(id="implement", title="Implementar", description=f"Implementar la solución: {goal}", assignee="code", depends_on=["design"]),
                TaskNode(id="test", title="Probar", description="Escribir y ejecutar pruebas", assignee="tester", depends_on=["implement"]),
                TaskNode(id="review", title="Revisar", description="Code review y optimización", assignee="optimizer", depends_on=["test"]),
            ])
        elif any(kw in goal_lower for kw in ["investigar", "research", "buscar", "search", "analizar", "analyze"]):
            tasks.extend([
                TaskNode(id="scope", title="Definir alcance", description="Definir preguntas clave y fuentes de información", assignee="scholar"),
                TaskNode(id="collect", title="Recopilar", description="Buscar y recopilar información relevante", assignee="scholar", depends_on=["scope"]),
                TaskNode(id="analyze", title="Analizar", description="Analizar datos y encontrar patrones", assignee="analyst", depends_on=["collect"]),
                TaskNode(id="report", title="Reportar", description="Generar informe con hallazgos y recomendaciones", assignee="scholar", depends_on=["analyze"]),
            ])
        elif any(kw in goal_lower for kw in ["debug", "fix", "error", "bug", "problema", "resolver"]):
            tasks.extend([
                TaskNode(id="reproduce", title="Reproducir", description="Reproducir el error y entender el contexto", assignee="debugger"),
                TaskNode(id="diagnose", title="Diagnosticar", description="Identificar la causa raíz del problema", assignee="debugger", depends_on=["reproduce"]),
                TaskNode(id="fix", title="Corregir", description="Implementar la corrección", assignee="code", depends_on=["diagnose"]),
                TaskNode(id="verify", title="Verificar", description="Verificar que el fix funciona y no rompe nada", assignee="tester", depends_on=["fix"]),
            ])
        elif any(kw in goal_lower for kw in ["deploy", "desplegar", "configurar", "configure", "setup"]):
            tasks.extend([
                TaskNode(id="prep", title="Preparar", description="Verificar requisitos y dependencias", assignee="devops"),
                TaskNode(id="config", title="Configurar", description="Configurar entorno y variables", assignee="devops", depends_on=["prep"]),
                TaskNode(id="deploy", title="Desplegar", description="Ejecutar despliegue", assignee="devops", depends_on=["config"]),
                TaskNode(id="verify_deploy", title="Verificar", description="Verificar que el despliegue funciona correctamente", assignee="tester", depends_on=["deploy"]),
            ])
        else:
            # Generic: single task or simple decomposition
            words = goal.split()
            if len(words) <= 10:
                tasks.append(TaskNode(id="execute", title="Ejecutar", description=goal, assignee="director"))
            else:
                tasks.extend([
                    TaskNode(id="understand", title="Entender", description="Analizar y entender el objetivo", assignee="director"),
                    TaskNode(id="execute", title="Ejecutar", description=goal, assignee="director", depends_on=["understand"]),
                ])

        # Assign IDs sequentially
        for i, task in enumerate(tasks):
            task.id = f"t{i+1:02d}_{task.id}"
            # Update depends_on with new IDs
            old_deps = task.depends_on.copy()
            task.depends_on = []
            for dep in old_deps:
                for t in tasks:
                    if t.id.endswith(f"_{dep}"):
                        task.depends_on.append(t.id)

        return tasks

    async def execute_dag(self, dag: TaskDAG, executor_func=None, max_iterations: int = 100) -> Dict:
        """Execute a DAG with TaskQueue for dependency resolution and parallel execution"""
        dag.status = "running"
        dag.started_at = datetime.now().isoformat()

        # Crear TaskQueue con las tareas del DAG
        task_queue = TaskQueue(max_parallel=dag.max_parallel)

        for node in dag.nodes:
            priority = TaskPriority.HIGH if node.assignee in ("code", "debugger") else TaskPriority.NORMAL
            task_queue.add_task(
                task_id=node.id,
                name=node.title,
                description=node.description,
                priority=priority,
                dependencies=node.depends_on,
                max_retries=node.max_retries,
                executor=None,  # Usamos executor_func externo
            )

        # Ejecutar usando TaskQueue
        async def run_node(task_id, node):
            for attempt in range(node.max_retries + 1):
                try:
                    if executor_func:
                        result = await asyncio.wait_for(
                            executor_func(node.description, node.assignee),
                            timeout=300.0
                        )
                        node.result = result.get("content", "") if isinstance(result, dict) else str(result)
                        node.status = TaskStatus.COMPLETED
                        node.completed_at = datetime.now().isoformat()
                        return result
                    else:
                        node.status = TaskStatus.COMPLETED
                        node.result = "Executed (no executor)"
                        node.completed_at = datetime.now().isoformat()
                        return {"content": node.result}
                except asyncio.TimeoutError:
                    node.error = "Execution timeout"
                    node.retries = attempt + 1
                    if attempt >= node.max_retries:
                        node.status = TaskStatus.FAILED
                        node.completed_at = datetime.now().isoformat()
                        raise
                    logger.warning(f"Task {node.id} attempt {attempt + 1} timeout")
                    await asyncio.sleep(1 * (attempt + 1))
                except Exception as e:
                    # Clasificar el error para decidir reintento/abort (antes se
                    # llamaba a `envelope.classify()`, metodo inexistente: la
                    # excepcion dejaba la tarea PENDING y el DAG "completed").
                    envelope = ErrorClassifier.classify(e)
                    node.error = envelope.message
                    node.retries = attempt + 1
                    
                    # Decision basada en ErrorEnvelope
                    if envelope.decision == ErrorDecision.ABORT:
                        node.status = TaskStatus.FAILED
                        node.completed_at = datetime.now().isoformat()
                        logger.critical(f"Task {node.id} aborted: {envelope.message}")
                        raise
                    
                    if attempt >= node.max_retries:
                        node.status = TaskStatus.FAILED
                        node.completed_at = datetime.now().isoformat()
                        raise
                    logger.warning(f"Task {node.id} attempt {attempt + 1} failed: {envelope.category.value} ({envelope.severity.value})")
                    await asyncio.sleep(1 * (attempt + 1))
            return {"content": "", "error": "Max retries exceeded"}

        # Ejecutar por niveles de paralelismo (un fallo bloquea a sus dependientes)
        levels = task_queue.get_parallel_groups()
        blocked: set[str] = set()
        for level in levels:
            nodes_in_level = [n for n in dag.nodes if n.id in level]
            coros = []
            for node in nodes_in_level:
                if node.status != TaskStatus.PENDING:
                    continue
                if any(dep in blocked for dep in node.depends_on):
                    node.status = TaskStatus.SKIPPED
                    node.error = "dependencia fallida"
                    blocked.add(node.id)
                    continue
                coros.append(run_node(node.id, node))
            if coros:
                await asyncio.gather(*coros, return_exceptions=True)
            blocked |= {
                n.id for n in dag.nodes
                if n.status in (TaskStatus.FAILED, TaskStatus.SKIPPED)
            }

        dag.completed_at = datetime.now().isoformat()
        dag.status = "completed" if not dag.get_failed_tasks() else "partial"

        return self._summarize_dag(dag)

    async def _execute_task(self, task: TaskNode, executor_func) -> Dict:
        """Execute a single task with retry logic"""
        for attempt in range(task.max_retries + 1):
            try:
                result = await executor_func(task.description, task.assignee)
                return result
            except Exception as e:
                # Clasificar error (ver nota en run_node: classify() era inexistente)
                envelope = ErrorClassifier.classify(e)
                task.retries = attempt + 1
                
                if envelope.decision == ErrorDecision.ABORT:
                    logger.critical(f"Task {task.id} aborted: {envelope.message}")
                    raise
                
                if attempt >= task.max_retries:
                    raise
                logger.warning(f"Task {task.id} attempt {attempt + 1} failed: {envelope.category.value}")
                await asyncio.sleep(1 * (attempt + 1))
        return {"content": "", "error": "Max retries exceeded"}

    def _summarize_dag(self, dag: TaskDAG) -> Dict:
        """Summarize DAG execution results"""
        results = {}
        for node in dag.nodes:
            results[node.id] = {
                "title": node.title,
                "status": node.status.value,
                "result": node.result[:500] if node.result else "",
                "error": node.error,
            }

        return {
            "dag_id": dag.id,
            "goal": dag.goal,
            "status": dag.status,
            "completion_percent": dag.get_completion_percent(),
            "total_tasks": len(dag.nodes),
            "completed": sum(1 for n in dag.nodes if n.status == TaskStatus.COMPLETED),
            "failed": sum(1 for n in dag.nodes if n.status == TaskStatus.FAILED),
            "skipped": sum(1 for n in dag.nodes if n.status == TaskStatus.SKIPPED),
            "results": results,
        }

    def get_run(self, run_id: str) -> Optional[TaskDAG]:
        return self._runs.get(run_id)

    def list_runs(self) -> List[Dict]:
        return [
            {
                "id": dag.id,
                "goal": dag.goal[:80],
                "status": dag.status,
                "completion": dag.get_completion_percent(),
                "tasks": len(dag.nodes),
                "created_at": dag.created_at,
            }
            for dag in sorted(self._runs.values(), key=lambda d: d.created_at, reverse=True)
        ]

    def get_stats(self) -> Dict:
        total = len(self._runs)
        completed = sum(1 for d in self._runs.values() if d.status == "completed")
        running = sum(1 for d in self._runs.values() if d.status == "running")
        return {
            "total_runs": total,
            "completed": completed,
            "running": running,
            "failed": total - completed - running,
        }
