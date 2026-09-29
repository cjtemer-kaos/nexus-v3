"""
ComposeWorkflow — Specs-driven autonomous development workflow.

Phases: SPEC → PLAN → BUILD → TEST → REVIEW → MERGE → REFLECT

- Recibe una especificacion (spec)
- La descompone en tareas paralelizables vía SubAgentSpawner
- Cada tarea se ejecuta con una gema especializada
- JudgePipeline evalua calidad en REVIEW
- Persiste estado en SQLite
"""

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("nexus-compose")


class ComposePhase(str, Enum):
    SPEC = "spec"
    PLAN = "plan"
    BUILD = "build"
    TEST = "test"
    REVIEW = "review"
    MERGE = "merge"
    REFLECT = "reflect"


PHASE_ORDER = list(ComposePhase)


class ComposeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class ComposeTask:
    id: str = ""
    title: str = ""
    description: str = ""
    assignee: str = "code"
    size: str = "M"
    depends_on: List[str] = field(default_factory=list)
    status: str = "pending"
    output: str = ""
    error: str = ""
    artifacts: Dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    completed_at: str = ""


@dataclass
class ComposeRun:
    id: str = ""
    spec: str = ""
    goal: str = ""
    project: str = "default"
    status: ComposeStatus = ComposeStatus.PENDING
    current_phase: ComposePhase = ComposePhase.SPEC
    phases: Dict[str, Any] = field(default_factory=dict)
    tasks: List[ComposeTask] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    summary: str = ""
    artifacts: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:8]
        if not self.created_at:
            now = datetime.now().isoformat()
            self.created_at = now
            self.updated_at = now


class ComposeWorkflow:

    def __init__(self, director=None, db_path: Optional[str] = None):
        self.director = director
        if db_path is None:
            db_path = str(Path.home() / ".nexus" / "brain" / "compose.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._runs: Dict[str, ComposeRun] = {}

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS compose_runs (
            id TEXT PRIMARY KEY,
            spec TEXT NOT NULL,
            goal TEXT DEFAULT '',
            project TEXT DEFAULT 'default',
            status TEXT NOT NULL DEFAULT 'pending',
            current_phase TEXT DEFAULT 'spec',
            phases_json TEXT DEFAULT '{}',
            tasks_json TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            summary TEXT DEFAULT '',
            artifacts_json TEXT DEFAULT '{}',
            metadata_json TEXT DEFAULT '{}'
        )""")
        conn.commit()
        conn.close()

    def _save_run(self, run: ComposeRun):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        run.updated_at = datetime.now().isoformat()
        c.execute("""INSERT OR REPLACE INTO compose_runs
            (id, spec, goal, project, status, current_phase, phases_json,
             tasks_json, created_at, updated_at, summary, artifacts_json, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            run.id, run.spec, run.goal, run.project, run.status.value,
            run.current_phase.value, json.dumps(run.phases, ensure_ascii=False),
            json.dumps([t.__dict__ for t in run.tasks], ensure_ascii=False),
            run.created_at, run.updated_at, run.summary,
            json.dumps(run.artifacts, ensure_ascii=False),
            json.dumps(run.metadata, ensure_ascii=False),
        ))
        conn.commit()
        conn.close()

    def _load_run(self, run_id: str) -> Optional[ComposeRun]:
        if run_id in self._runs:
            return self._runs[run_id]
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM compose_runs WHERE id = ?", (run_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        tasks = [ComposeTask(**t) for t in json.loads(row["tasks_json"] or "[]")]
        run = ComposeRun(
            id=row["id"], spec=row["spec"], goal=row.get("goal", ""),
            project=row.get("project", "default"),
            status=ComposeStatus(row["status"]),
            current_phase=ComposePhase(row["current_phase"]),
            phases=json.loads(row["phases_json"] or "{}"),
            tasks=tasks,
            created_at=row["created_at"], updated_at=row["updated_at"],
            summary=row.get("summary", ""),
            artifacts=json.loads(row["artifacts_json"] or "{}"),
            metadata=json.loads(row["metadata_json"] or "{}"),
        )
        self._runs[run_id] = run
        return run

    async def create(self, spec: str, goal: str = "", project: str = "default") -> ComposeRun:
        run = ComposeRun(spec=spec, goal=goal or spec[:100], project=project)
        self._runs[run.id] = run
        self._save_run(run)
        logger.info(f"Compose run created: {run.id}")
        return run

    async def execute(self, run_id: str) -> ComposeRun:
        run = self._load_run(run_id)
        if not run:
            raise ValueError(f"Run not found: {run_id}")

        run.status = ComposeStatus.RUNNING
        self._save_run(run)
        t0 = time.time()

        for phase in PHASE_ORDER:
            run.current_phase = phase
            self._save_run(run)
            logger.info(f"Compose {run.id}: phase {phase.value}")

            try:
                result = await self._execute_phase(phase, run)
                run.phases[phase.value] = result
                if result.get("error"):
                    run.status = ComposeStatus.FAILED
                    run.phases[phase.value] = result
                    break
            except Exception as e:
                logger.exception(f"Compose {run.id} failed at {phase.value}: {e}")
                run.phases[phase.value] = {"error": str(e)}
                run.status = ComposeStatus.FAILED
                break

        if run.status == ComposeStatus.RUNNING:
            run.status = ComposeStatus.COMPLETED

        run.metadata["duration_s"] = round(time.time() - t0, 2)
        self._save_run(run)
        return run

    async def _execute_phase(self, phase: ComposePhase, run: ComposeRun) -> Dict:
        handler = getattr(self, f"_phase_{phase.value}", None)
        if handler:
            return await handler(run)
        return {"status": "skipped", "note": f"no handler for {phase.value}"}

    async def _phase_spec(self, run: ComposeRun) -> Dict:
        if not self.director:
            return {"spec": run.spec, "status": "ok"}
        prompt = (
            "Analiza la siguiente especificacion y extrae:\n"
            "1. Objetivo principal (1 linea)\n"
            "2. Requisitos funcionales (lista)\n"
            "3. Requisitos tecnicos (lista)\n"
            "4. Criterios de exito (lista)\n"
            "5. Riesgos (lista)\n\n"
            f"Especificacion:\n{run.spec}\n\n"
            "Formato JSON."
        )
        return await self._llm_call(run, prompt)

    async def _phase_plan(self, run: ComposeRun) -> Dict:
        spec_out = run.phases.get("spec", {})
        prompt = (
            "Genera un plan de ejecucion con tareas concretas.\n"
            "Cada tarea: title, description, assignee (code/tester/sage/security/devops), size (S/M/L), depends_on (ids).\n"
            "Identifica que tareas pueden ejecutarse en paralelo.\n"
            "Maximo 8 tareas.\n\n"
            f"Objetivo:\n{run.goal}\n\n"
            f"Especificacion:\n{json.dumps(spec_out, ensure_ascii=False)[:2000]}\n\n"
            "Formato: JSON array."
        )
        result = await self._llm_call(run, prompt)
        tasks_data = result.get("parsed")
        if not isinstance(tasks_data, list):
            tasks_data = self._extract_tasks_from_raw(result.get("raw", ""))
        for td in tasks_data:
            task = ComposeTask(
                id=str(uuid.uuid4())[:8],
                title=td.get("title", "Untitled"),
                description=td.get("description", ""),
                assignee=td.get("assignee", "code"),
                size=td.get("size", "M"),
                depends_on=td.get("depends_on", []),
            )
            run.tasks.append(task)
        self._save_run(run)
        result["tasks_count"] = len(tasks_data)
        return result

    def _extract_tasks_from_raw(self, raw: str) -> List[Dict]:
        parsed = self._try_parse_json(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "tasks" in parsed:
            return parsed["tasks"]
        return [{"title": "Implement", "description": raw[:200], "assignee": "code"}]

    async def _phase_build(self, run: ComposeRun) -> Dict:
        if not run.tasks:
            return {"status": "no_tasks", "tasks": []}

        results = []
        for task in run.tasks:
            task.status = "running"
            task.started_at = datetime.now().isoformat()
            self._save_run(run)

            prompt = (
                f"Ejecuta esta tarea del plan:\n\n"
                f"Titulo: {task.title}\n"
                f"Descripcion: {task.description}\n"
                f"Asignado a: {task.assignee}\n\n"
                f"Contexto del proyecto: {run.goal}\n"
                f"Especificacion: {run.spec[:2000]}\n\n"
                "Genera el codigo o configuracion necesario. "
                "Incluye la ruta del archivo y el contenido."
            )

            task_result = await self._llm_call(run, prompt)
            task.output = task_result.get("raw", "")
            task.status = "completed" if not task_result.get("error") else "failed"
            task.completed_at = datetime.now().isoformat()
            self._save_run(run)
            results.append({
                "task_id": task.id,
                "title": task.title,
                "status": task.status,
            })

        return {"status": "ok", "tasks": results}

    async def _phase_test(self, run: ComposeRun) -> Dict:
        build_out = run.phases.get("build", {})
        prompt = (
            "Define casos de test para verificar la implementacion.\n\n"
            f"Objetivo: {run.goal}\n"
            f"Tareas completadas: {json.dumps([t.__dict__ for t in run.tasks if t.status == 'completed'], ensure_ascii=False)[:2000]}\n\n"
            "Para cada caso: nombre, descripcion, pasos, resultado esperado, tipo (unit/integration/e2e).\n"
            "Formato JSON."
        )
        return await self._llm_call(run, prompt)

    async def _phase_review(self, run: ComposeRun) -> Dict:
        prompt = (
            "Eres un staff engineer haciendo code review. Evalua:\n\n"
            f"Objetivo: {run.goal}\n"
            f"Especificacion: {run.spec[:1500]}\n"
            f"Tareas: {json.dumps([{'title': t.title, 'output': t.output[:500]} for t in run.tasks], ensure_ascii=False)[:3000]}\n\n"
            "Criterios:\n"
            "1. Correctitud: resuelve el problema?\n"
            "2. Seguridad: vulnerabilidades?\n"
            "3. Performance: bottlenecks?\n"
            "4. Mantenibilidad: codigo limpio?\n"
            "5. Completitud: cubre todos los requisitos?\n\n"
            "Veredicto: approve / changes_requested / reject\n"
            "Si hay cambios, lista issues especificos con sugerencias.\n"
            "Formato JSON."
        )
        result = await self._llm_call(run, prompt)
        verdict = ""
        if isinstance(result.get("parsed"), dict):
            verdict = result["parsed"].get("verdict", "")
        if verdict in ("reject", "changes_requested"):
            result["gate"] = "blocked"
            result["gate_reason"] = f"Review verdict: {verdict}"
        else:
            result["gate"] = "passed"
        return result

    async def _phase_merge(self, run: ComposeRun) -> Dict:
        artifacts = {}
        for task in run.tasks:
            if task.output:
                artifacts[f"task_{task.id}_{task.title[:20]}"] = task.output[:500]
        run.artifacts = artifacts
        self._save_run(run)

        prompt = (
            "Genera un resumen de integracion para este compose run:\n\n"
            f"Objetivo: {run.goal}\n"
            f"Tareas: {len(run.tasks)} ({sum(1 for t in run.tasks if t.status == 'completed')} completadas)\n"
            f"Especificacion: {run.spec[:1000]}\n\n"
            "Incluye:\n"
            "1. Resumen de lo implementado\n"
            "2. Archivos creados/modificados\n"
            "3. Instrucciones de deploy\n"
            "4. Commit message sugerido\n\n"
            "Formato JSON."
        )
        result = await self._llm_call(run, prompt)
        run.summary = result.get("raw", "")[:500]
        self._save_run(run)
        return result

    async def _phase_reflect(self, run: ComposeRun) -> Dict:
        prompt = (
            "Eres un engineering manager haciendo retrospectiva:\n\n"
            f"Objetivo: {run.goal}\n"
            f"Status: {run.status.value}\n"
            f"Fases completadas: {list(run.phases.keys())}\n"
            f"Duracion: {run.metadata.get('duration_s', 0):.1f}s\n\n"
            "Responde:\n"
            "1. Que salio bien (top 3)\n"
            "2. Que mejorar (top 3)\n"
            "3. Score general (1-10)\n"
            "4. Lecciones aprendidas\n\n"
            "Formato JSON."
        )
        return await self._llm_call(run, prompt)

    async def _llm_call(self, run: ComposeRun, prompt: str) -> Dict:
        if not self.director:
            return {"raw": f"[mock] {prompt[:100]}...", "parsed": None, "status": "mock"}
        try:
            gema = self.director.gemas.get("code" if run.current_phase in (ComposePhase.BUILD, ComposePhase.TEST) else "architect")
            if not gema:
                gema = list(self.director.gemas.values())[0]
            result = await self.director.execute(gema_name=gema.name, task=prompt, session_project=run.project)
            raw = result.get("response", result.get("result", str(result)))
            return {"raw": raw, "parsed": self._try_parse_json(raw), "status": "ok"}
        except Exception as e:
            return {"raw": "", "parsed": None, "error": str(e), "status": "error"}

    def _try_parse_json(self, text: str) -> Any:
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            start = next((i for i, line in enumerate(lines) if line.strip().startswith("```")), 0)
            text = "\n".join(lines[start + 1:])
            end = text.rfind("```")
            if end >= 0:
                text = text[:end].strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

    def get_run(self, run_id: str) -> Optional[ComposeRun]:
        return self._load_run(run_id)

    def list_runs(self, project: str = None, limit: int = 20) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if project:
            c.execute("SELECT id, spec, goal, status, current_phase, created_at, updated_at, summary FROM compose_runs WHERE project = ? ORDER BY created_at DESC LIMIT ?", (project, limit))
        else:
            c.execute("SELECT id, spec, goal, status, current_phase, created_at, updated_at, summary FROM compose_runs ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def get_stats(self) -> Dict:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM compose_runs")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM compose_runs WHERE status = 'completed'")
        completed = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM compose_runs WHERE status = 'running'")
        running = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM compose_runs WHERE status = 'failed'")
        failed = c.fetchone()[0]
        conn.close()
        return {
            "total_runs": total,
            "completed": completed,
            "running": running,
            "failed": failed,
            "db_path": self.db_path,
        }
