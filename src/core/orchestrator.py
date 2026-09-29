"""
NexusOrchestrator — Orquestador Multi-Motor con descomposición LLM.

Patrón absorbido de open-multi-agent:
  Coordinator: decompose goal → TaskDAG → execute queue → synthesize

Capas:
  - TaskQueue + DAGCoordinator (existente) para ejecución con dependencias
  - ProviderRegistry + AgentRunner (nuevo) para ejecución por tarea
  - LLM-based decomposition y synthesis (nuevo)

Flujo:
  1. decompose(goal) → TaskDAG  (LLM coordinator descompone)
  2. execute(dag) → resultados   (TaskQueue + AgentRunner)
  3. synthesize(goal, results) → respuesta final (LLM coordinator sintetiza)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from src.core.provider_base import LLMMessage
from src.core.agent_runner import AgentRunner, AgentRunSpec
from src.core.dag_coordinator import DAGCoordinator, TaskDAG, TaskNode, TaskStatus
from src.core.task_queue import TaskQueue

logger = logging.getLogger(__name__)

COORDINATOR_DECOMPOSE_PROMPT = """Eres un coordinador de tareas experto. Descompón el siguiente objetivo en tareas concretas y accionables, asignándolas al agente correcto del equipo.

## Equipo disponible
{roster}

## Reglas
1. Cada tarea debe tener un título claro y una descripción que indique qué producir
2. Asigna cada tarea al agente más capacitado según su especialidad
3. Define dependencias entre tareas SOLO cuando una tarea necesita el resultado de otra
4. Prefiere paralelismo: tareas independientes deben poder ejecutarse simultáneamente
5. Máximo 8 tareas. Sé específico, no genérico

## Formato de salida
Responde ÚNICAMENTE con un JSON array. Sin texto adicional, sin markdown:

```json
[
  {{
    "title": "Título corto",
    "description": "Descripción detallada con objetivo y entregable",
    "assignee": "nombre_del_agente",
    "depends_on": ["titulo_de_tarea_anterior"]
  }}
]
```

Objetivo: {goal}"""

COORDINATOR_SYNTHESIZE_PROMPT = """Eres un coordinador de tareas experto. Sintetiza los resultados de todas las tareas ejecutadas para generar una respuesta final completa y coherente.

## Objetivo original
{goal}

## Resultados de tareas
{task_results}

## Instrucciones
1. Integra la información de todas las tareas en una respuesta unificada
2. Si hay tareas fallidas, menciónalo y ofrece alternativas
3. Prioriza la claridad y completitud sobre la brevedad
4. Si el objetivo lo requiere, incluye pasos siguientes recomendados

Genera la respuesta final directamente, sin metadatos adicionales."""


@dataclass
class OrchestratorConfig:
    provider_registry: Any
    tool_executor: Callable[[str, dict], Any] | None = None
    get_tool_schemas: Callable[[], list[dict]] | None = None
    max_iterations_per_task: int = 5
    max_concurrent_tasks: int = 3
    coordinator_model: str = "qwen2.5-coder:7b"
    coordinator_provider: str = "ollama-gema"


class OrchestrationResult:
    dag: TaskDAG
    task_results: dict[str, dict]
    synthesis: str
    success: bool
    duration_s: float


class NexusOrchestrator:
    """Orquestador que descompone objetivos con LLM, ejecuta con TaskQueue + AgentRunner, y sintetiza."""

    def __init__(self, config: OrchestratorConfig):
        self.config = config
        self.dag_coordinator = DAGCoordinator()
        self._dags: dict[str, TaskDAG] = {}
        self._max_dags = 50  # Cap to prevent memory leak
        self._run_count = 0
        self._total_duration_s = 0.0

    async def decompose(self, goal: str) -> TaskDAG:
        """Descompone un objetivo en TaskDAG usando LLM como coordinator."""
        provider = self.config.provider_registry.get(self.config.coordinator_provider)
        if not provider:
            logger.warning("No coordinator provider, usando descomposición por patrón")
            return self.dag_coordinator.decompose_goal(goal)

        try:
            runner = AgentRunner(provider, tool_executor=self.config.tool_executor)
            spec = AgentRunSpec(
                messages=[LLMMessage(role="user", content=COORDINATOR_DECOMPOSE_PROMPT.format(
                    roster=self._build_roster(),
                    goal=goal,
                ))],
                tools_definitions=[],
                max_iterations=2,
            )
            result = await runner.run(spec)
            if result.stop_reason == "error" or not result.content:
                raise ValueError(f"Coordinator failed: {result.error}")

            tasks_data = self._parse_json_from_response(result.content)
            nodes = self._tasks_to_nodes(tasks_data)
        except Exception as e:
            logger.warning(f"LLM decomposition failed ({e}), usando patrón")
            return self.dag_coordinator.decompose_goal(goal)

        dag = TaskDAG(
            id=str(uuid.uuid4())[:8],
            goal=goal,
            nodes=nodes,
            max_parallel=self.config.max_concurrent_tasks,
        )
        self._dags[dag.id] = dag
        # Evict oldest when cap exceeded
        if len(self._dags) > self._max_dags:
            oldest_id = next(iter(self._dags))
            if oldest_id != dag.id:
                del self._dags[oldest_id]
        logger.info(f"LLM DAG created: {dag.id} with {len(nodes)} tasks")
        return dag

    async def execute(self, dag: TaskDAG) -> dict[str, dict]:
        """Ejecuta un TaskDAG usando TaskQueue + AgentRunner."""
        task_queue = TaskQueue(max_parallel=dag.max_parallel)
        executor_map: dict[str, Callable] = {}

        for node in dag.nodes:
            async def make_executor(n: TaskNode) -> Callable:
                async def execute_node(**kwargs) -> str:
                    return await self._execute_task_node(n)
                return execute_node

            fn = await make_executor(node)
            executor_map[node.id] = fn

            deps = [d for d in node.depends_on]
            task_queue.add_task(
                task_id=node.id,
                name=node.title,
                description=node.description,
                executor=fn,
                dependencies=deps or None,
            )

        raw_results = await task_queue.execute_all()

        task_results = {}
        for node in dag.nodes:
            node_result = raw_results.get(node.id, "")
            status = "completed" if node.status == TaskStatus.COMPLETED else "failed"
            task_results[node.id] = {
                "title": node.title,
                "description": node.description,
                "assignee": node.assignee,
                "status": status,
                "result": (node_result[:2000] if isinstance(node_result, str) else str(node_result)) if node_result else "",
                "error": node.error or "",
            }

        return task_results

    async def synthesize(self, goal: str, task_results: dict[str, dict]) -> str:
        """Sintetiza resultados de tareas en respuesta final usando LLM."""
        provider = self.config.provider_registry.get(self.config.coordinator_provider)
        if not provider:
            return self._fallback_synthesize(task_results)

        results_text = "\n\n".join(
            f"### {r['title']} ({r['assignee']}, {r['status']})\n"
            f"{r['result'] or '(sin resultado)'}"
            + (f"\nError: {r['error']}" if r.get("error") else "")
            for r in task_results.values()
        )

        try:
            runner = AgentRunner(provider, tool_executor=self.config.tool_executor)
            spec = AgentRunSpec(
                messages=[LLMMessage(role="user", content=COORDINATOR_SYNTHESIZE_PROMPT.format(
                    goal=goal, task_results=results_text,
                ))],
                tools_definitions=[],
                max_iterations=2,
            )
            result = await runner.run(spec)
            if result.stop_reason != "error" and result.content:
                return result.content
        except Exception as e:
            logger.warning(f"Synthesis LLM failed ({e})")

        return self._fallback_synthesize(task_results)

    async def orchestrate(self, goal: str) -> OrchestrationResult:
        """Pipeline completo: decompose → execute → synthesize."""
        start = datetime.now()
        dag = await self.decompose(goal)
        task_results = await self.execute(dag)
        synthesis = await self.synthesize(goal, task_results)
        duration = (datetime.now() - start).total_seconds()

        result = OrchestrationResult()
        result.dag = dag
        result.task_results = task_results
        result.synthesis = synthesis
        result.success = all(r["status"] == "completed" for r in task_results.values())
        result.duration_s = duration
        self._run_count += 1
        self._total_duration_s += duration
        logger.info(f"Orchestration #{self._run_count} complete: {len(task_results)} tasks in {duration:.1f}s")
        return result

    def status(self) -> dict:
        """Estado actual del orquestador."""
        return {
            "run_count": self._run_count,
            "total_duration_s": round(self._total_duration_s, 2),
            "avg_duration_s": round(self._total_duration_s / max(self._run_count, 1), 2),
            "dags_tracked": len(self._dags),
            "config": {
                "max_iterations_per_task": self.config.max_iterations_per_task,
                "max_concurrent_tasks": self.config.max_concurrent_tasks,
                "coordinator_provider": self.config.coordinator_provider,
                "coordinator_model": self.config.coordinator_model,
                "has_tool_executor": self.config.tool_executor is not None,
                "has_tool_schemas": self.config.get_tool_schemas is not None,
            },
        }

    async def _execute_task_node(self, node: TaskNode) -> str:
        """Ejecuta una tarea individual via AgentRunner + ProviderRegistry."""
        provider_name = self._assign_provider(node.assignee)
        provider = self.config.provider_registry.get(provider_name)
        if not provider:
            provider = self.config.provider_registry.get("ollama-local")
        if not provider:
            node.status = TaskStatus.FAILED
            node.error = f"No provider for {node.assignee}"
            return ""

        node.status = TaskStatus.RUNNING
        node.started_at = datetime.now().isoformat()

        try:
            tool_schemas = self.config.get_tool_schemas() if self.config.get_tool_schemas else []
            runner = AgentRunner(provider, tool_executor=self.config.tool_executor)
            spec = AgentRunSpec(
                messages=[LLMMessage(role="user", content=node.description)],
                tools_definitions=tool_schemas,
                max_iterations=self.config.max_iterations_per_task,
            )
            result = await runner.run(spec)
            if result.stop_reason == "error":
                node.status = TaskStatus.FAILED
                node.error = result.error or "Unknown error"
                node.completed_at = datetime.now().isoformat()
                return ""
            node.status = TaskStatus.COMPLETED
            node.result = result.content or ""
            node.completed_at = datetime.now().isoformat()
            return result.content or ""
        except Exception as e:
            node.status = TaskStatus.FAILED
            node.error = str(e)
            node.completed_at = datetime.now().isoformat()
            logger.exception(f"Task {node.id} execution failed")
            return ""

    def _assign_provider(self, gema: str) -> str:
        """Mapea nombre de gema a proveedor registrado."""
        gema_to_provider = {
            "code": "ollama-gema",
            "architect": "ollama-gema",
            "tester": "ollama-gema",
            "director": "gema-con-fallback",
            "scholar": "gema-con-fallback",
            "analyst": "ollama-local",
            "debugger": "ollama-gema",
            "optimizer": "ollama-gema",
            "devops": "ollama-local",
        }
        return gema_to_provider.get(gema, "ollama-local")

    def _build_roster(self) -> str:
        return """| Agente | Especialidad |
|---|---|
| director | Orquestación, planificación, liderazgo |
| code | Programación, refactorización, implementación |
| scholar | Investigación, búsqueda, análisis |
| architect | Diseño de sistemas, infraestructura |
| analyst | Análisis de datos, métricas |
| debugger | Debugging, troubleshooting |
| optimizer | Optimización, performance |
| tester | Testing, QA, validación |
| devops | Despliegue, configuración, CI/CD |"""

    def _parse_json_from_response(self, content: str) -> list[dict]:
        """Extrae JSON de la respuesta del LLM."""
        content = content.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            start = -1
            for i, line in enumerate(lines):
                if line.strip().startswith("```"):
                    start = i
                    break
            if start >= 0:
                content = "\n".join(lines[start + 1:])
                end_idx = content.rfind("```")
                if end_idx >= 0:
                    content = content[:end_idx].strip()
        return json.loads(content)

    def _tasks_to_nodes(self, tasks_data: list[dict]) -> list[TaskNode]:
        """Convierte JSON de tareas a lista de TaskNode."""
        nodes = []
        for i, t in enumerate(tasks_data):
            title = t.get("title", f"Task {i+1}")
            description = t.get("description", title)
            assignee = t.get("assignee", "director")
            depends_on = t.get("depends_on", [])

            old_deps = depends_on.copy() if depends_on else []
            resolved_deps = []
            if old_deps:
                dep_titles = {n.title: n.id for n in nodes}
                for dep_title in old_deps:
                    if dep_title in dep_titles:
                        resolved_deps.append(dep_titles[dep_title])

            node_id = f"t{i+1:02d}_{title.lower().replace(' ', '_')[:20]}"
            node = TaskNode(
                id=node_id,
                title=title,
                description=description,
                assignee=assignee,
                depends_on=resolved_deps,
            )
            nodes.append(node)
        return nodes

    def _fallback_synthesize(self, task_results: dict[str, dict]) -> str:
        lines = ["## Resultados de ejecución\n"]
        for r in task_results.values():
            status_icon = "✓" if r["status"] == "completed" else "✗"
            lines.append(f"{status_icon} **{r['title']}** ({r['assignee']})")
            if r.get("result"):
                lines.append(f"  {r['result'][:300]}")
            if r.get("error"):
                lines.append(f"  Error: {r['error']}")
        return "\n".join(lines)
