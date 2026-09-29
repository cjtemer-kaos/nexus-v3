"""
DevLoop — 7-Phase Development Loop (gstack pattern).

THINK -> PLAN -> BUILD -> REVIEW -> TEST -> SHIP -> REFLECT

Cada fase tiene:
  - gate de entrada (precondiciones)
  - executor (quien hace el trabajo)
  - gate de salida (validacion antes de avanzar)
  - rollback (si falla el gate de salida)

Inspirado en gstack (Garry Tan / YC, marzo 2026).
Adaptado para NEXUS: usa LLM local para gates, Actor model para ejecucion.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class Phase(str, Enum):
    THINK = "think"
    PLAN = "plan"
    BUILD = "build"
    REVIEW = "review"
    TEST = "test"
    SHIP = "ship"
    REFLECT = "reflect"


PHASE_ORDER = list(Phase)


class GateResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class PhaseOutput:
    phase: Phase
    started_at: str = ""
    completed_at: str = ""
    duration_s: float = 0.0
    gate_result: GateResult = GateResult.PASS
    gate_reason: str = ""
    output: Any = None
    error: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)


@dataclass
class DevLoopRun:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    goal: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    current_phase: Phase = Phase.THINK
    phases: dict[str, PhaseOutput] = field(default_factory=dict)
    status: str = "running"  # running | completed | failed | blocked
    total_duration_s: float = 0.0

    def summary(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal[:100],
            "status": self.status,
            "current_phase": self.current_phase.value,
            "phases_completed": [
                p for p, o in self.phases.items()
                if o.gate_result == GateResult.PASS
            ],
            "total_duration_s": round(self.total_duration_s, 2),
        }


# --- Phase-specific prompts ---

PHASE_PROMPTS: dict[Phase, str] = {
    Phase.THINK: """Eres un asesor tecnico senior. Analiza este objetivo y genera un design document conciso:

## Objetivo
{goal}

## Contexto del proyecto
{context}

Responde con:
1. **Problema**: que se resuelve (1-2 lineas)
2. **Solucion propuesta**: enfoque tecnico (3-5 lineas)
3. **Riesgos**: top 3 riesgos y mitigaciones
4. **Dependencias**: que se necesita antes de construir
5. **Criterios de exito**: como sabemos que funciona

Formato: JSON con keys problem, solution, risks, dependencies, success_criteria.""",

    Phase.PLAN: """Eres un engineering manager. Revisa este design doc y genera un plan de ejecucion:

## Design Doc
{think_output}

## Reglas
- Maximo 8 tareas concretas
- Cada tarea: titulo, descripcion, assignee (code/architect/tester/devops), estimacion (S/M/L)
- Define dependencias entre tareas
- Identifica que se puede paralelizar

Formato: JSON array con keys title, description, assignee, size, depends_on.""",

    Phase.BUILD: """Ejecuta la tarea asignada segun el plan.

## Plan
{plan_output}

## Tarea actual
{current_task}

Genera el codigo/configuracion necesario. Se conciso y funcional.""",

    Phase.REVIEW: """Eres un staff engineer haciendo code review. Evalua:

## Objetivo original
{goal}

## Codigo producido
{build_output}

Criterios:
1. **Correctitud**: resuelve el problema?
2. **Seguridad**: vulnerabilidades?
3. **Performance**: bottlenecks obvios?
4. **Mantenibilidad**: legible, documentado?

Responde JSON: {{verdict: "approve"|"request_changes", issues: [...], suggestions: [...]}}""",

    Phase.TEST: """Eres QA engineer. Define y ejecuta tests para:

## Objetivo
{goal}

## Implementacion
{build_output}

Genera:
1. Test cases criticos (happy path + edge cases)
2. Comandos para ejecutar tests
3. Criterios pass/fail

Formato JSON: {{test_cases: [...], commands: [...], pass_criteria: "..."}}""",

    Phase.SHIP: """Eres release engineer. Prepara el ship:

## Objetivo
{goal}

## Review status
{review_output}

## Test results
{test_output}

Checklist:
1. Todos los tests pasan?
2. Review aprobado?
3. Breaking changes documentados?
4. Commit message draft
5. PR description draft

Formato JSON: {{ready: true|false, blockers: [...], commit_msg: "...", pr_description: "..."}}""",

    Phase.REFLECT: """Eres engineering manager haciendo retrospectiva:

## Objetivo
{goal}

## Resultado
{ship_output}

## Metricas del loop
{metrics}

Responde con:
1. **Que salio bien**: top 3
2. **Que mejorar**: top 3
3. **Lecciones aprendidas**: para el cerebro compartido
4. **Score**: 1-10 del proceso

Formato JSON: {{went_well: [...], improve: [...], lessons: [...], score: N}}""",
}


# --- Gate definitions ---

@dataclass
class GateConfig:
    """Configuracion de gate para cada fase."""
    required: bool = True
    auto_pass: bool = False  # skip gate check (for BUILD phase)
    max_retries: int = 2
    validator: Callable[[PhaseOutput], GateResult] | None = None


DEFAULT_GATES: dict[Phase, GateConfig] = {
    Phase.THINK: GateConfig(required=True),
    Phase.PLAN: GateConfig(required=True),
    Phase.BUILD: GateConfig(required=True, auto_pass=True),  # build siempre pasa si no hay error
    Phase.REVIEW: GateConfig(required=True, max_retries=2),
    Phase.TEST: GateConfig(required=True, max_retries=1),
    Phase.SHIP: GateConfig(required=True),
    Phase.REFLECT: GateConfig(required=False, auto_pass=True),
}


class DevLoop:
    """
    7-Phase Development Loop.

    Uso:
        loop = DevLoop(llm_call=my_llm_fn)
        result = await loop.run("Implementar circuit breaker para API calls")
    """

    def __init__(
        self,
        llm_call: Callable[[str], Awaitable[str]],
        gates: dict[Phase, GateConfig] | None = None,
        on_phase_complete: Callable[[Phase, PhaseOutput], Awaitable[None]] | None = None,
        context: str = "",
    ):
        self.llm_call = llm_call
        self.gates = gates or DEFAULT_GATES
        self.on_phase_complete = on_phase_complete
        self.context = context
        self._runs: list[DevLoopRun] = []

    async def run(self, goal: str, start_phase: Phase = Phase.THINK) -> DevLoopRun:
        """Ejecuta el loop completo desde start_phase."""
        run = DevLoopRun(goal=goal, current_phase=start_phase)
        self._runs.append(run)
        t0 = time.time()

        start_idx = PHASE_ORDER.index(start_phase)
        for phase in PHASE_ORDER[start_idx:]:
            run.current_phase = phase
            output = await self._execute_phase(phase, run)
            run.phases[phase.value] = output

            if output.error:
                run.status = "failed"
                logger.error(f"DevLoop {run.id} failed at {phase.value}: {output.error}")
                break

            # Gate check
            gate = self.gates.get(phase, GateConfig())
            if gate.auto_pass:
                output.gate_result = GateResult.PASS
            elif gate.required:
                gate_result = await self._check_gate(phase, output, gate)
                output.gate_result = gate_result
                if gate_result == GateResult.FAIL:
                    run.status = "blocked"
                    logger.warning(f"DevLoop {run.id} blocked at {phase.value} gate: {output.gate_reason}")
                    break

            if self.on_phase_complete:
                await self.on_phase_complete(phase, output)

        if run.status == "running":
            run.status = "completed"

        run.total_duration_s = time.time() - t0
        logger.info(f"DevLoop {run.id} finished: {run.status} in {run.total_duration_s:.1f}s")
        return run

    async def resume(self, run: DevLoopRun) -> DevLoopRun:
        """Resume un run bloqueado desde la fase actual."""
        if run.status != "blocked":
            return run
        run.status = "running"
        return await self.run(run.goal, start_phase=run.current_phase)

    async def _execute_phase(self, phase: Phase, run: DevLoopRun) -> PhaseOutput:
        """Ejecuta una fase individual."""
        output = PhaseOutput(phase=phase, started_at=datetime.now().isoformat())
        t0 = time.time()

        try:
            prompt = self._build_prompt(phase, run)
            response = await self.llm_call(prompt)
            output.output = self._try_parse_json(response) or response
        except Exception as e:
            output.error = str(e)
            logger.exception(f"Phase {phase.value} execution error")

        output.completed_at = datetime.now().isoformat()
        output.duration_s = time.time() - t0
        return output

    def _build_prompt(self, phase: Phase, run: DevLoopRun) -> str:
        """Construye el prompt para la fase con contexto acumulado."""
        template = PHASE_PROMPTS[phase]

        # Collect outputs from previous phases
        vars_map = {
            "goal": run.goal,
            "context": self.context,
            "think_output": self._get_phase_output_str(run, Phase.THINK),
            "plan_output": self._get_phase_output_str(run, Phase.PLAN),
            "build_output": self._get_phase_output_str(run, Phase.BUILD),
            "review_output": self._get_phase_output_str(run, Phase.REVIEW),
            "test_output": self._get_phase_output_str(run, Phase.TEST),
            "ship_output": self._get_phase_output_str(run, Phase.SHIP),
            "current_task": "",
            "metrics": json.dumps({
                "phases_completed": len(run.phases),
                "total_duration_s": sum(p.duration_s for p in run.phases.values()),
                "failed_gates": [
                    p for p, o in run.phases.items() if o.gate_result == GateResult.FAIL
                ],
            }),
        }

        # Safe format — ignore missing keys
        result = template
        for k, v in vars_map.items():
            result = result.replace(f"{{{k}}}", str(v))
        return result

    def _get_phase_output_str(self, run: DevLoopRun, phase: Phase) -> str:
        """Obtiene output de una fase como string truncado."""
        po = run.phases.get(phase.value)
        if not po or not po.output:
            return "(no disponible)"
        out = po.output
        if isinstance(out, dict):
            out = json.dumps(out, ensure_ascii=False, indent=2)
        return str(out)[:3000]

    async def _check_gate(self, phase: Phase, output: PhaseOutput, gate: GateConfig) -> GateResult:
        """Valida gate de salida de una fase."""
        # Custom validator first
        if gate.validator:
            return gate.validator(output)

        # Default: REVIEW gate checks for "approve" verdict
        if phase == Phase.REVIEW and isinstance(output.output, dict):
            verdict = output.output.get("verdict", "")
            if verdict == "approve":
                return GateResult.PASS
            output.gate_reason = f"Review verdict: {verdict}"
            return GateResult.FAIL

        # SHIP gate checks ready flag
        if phase == Phase.SHIP and isinstance(output.output, dict):
            if output.output.get("ready"):
                return GateResult.PASS
            blockers = output.output.get("blockers", [])
            output.gate_reason = f"Ship blockers: {blockers}"
            return GateResult.FAIL

        # Default: pass if no error
        if output.error:
            output.gate_reason = output.error
            return GateResult.FAIL
        return GateResult.PASS

    def _try_parse_json(self, text: str) -> dict | list | None:
        """Intenta parsear JSON de respuesta LLM."""
        text = text.strip()
        # Strip markdown code blocks
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

    def status(self) -> dict:
        return {
            "total_runs": len(self._runs),
            "runs": [r.summary() for r in self._runs[-5:]],
        }
