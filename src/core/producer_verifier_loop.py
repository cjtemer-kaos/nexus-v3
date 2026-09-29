"""
Producer+Verifier Adversarial Loop — inspirado en MiniMax Code.

Flujo:
1. Producer genera codigo/artefacto
2. Verifier verifica independientemente
3. Si FAIL, producer itera con feedback
4. Loop hasta PASS o max_iterations
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.evaluation.verifier_agent import Verdict

logger = logging.getLogger("producer-verifier")


@dataclass
class LoopResult:
    passed: bool
    iterations: int
    final_output: str = ""
    report: str = ""
    iteration_logs: list[dict] = field(default_factory=list)


class ProducerVerifierLoop:

    def __init__(
        self,
        producer: Callable,
        verifier: Callable,
        max_iterations: int = 5,
        llm_executor: Optional[Callable] = None,
    ):
        self._producer = producer
        self._verifier = verifier
        self._max_iterations = max_iterations
        self._llm = llm_executor

    async def run(
        self,
        task: str,
        success_criteria: str = "",
        context: Optional[dict] = None,
    ) -> LoopResult:
        iteration_logs = []
        current_output = ""
        context = context or {}

        for i in range(1, self._max_iterations + 1):
            logger.info(f"PV Loop iteration {i}/{self._max_iterations}")

            # Step 1: Producer generates
            producer_input = {
                "task": task,
                "success_criteria": success_criteria,
                "iteration": i,
                "feedback": iteration_logs[-1].get("feedback", "") if iteration_logs else "",
                **context,
            }
            current_output = await self._producer(producer_input)
            if not current_output:
                current_output = ""

            # Step 2: Verifier checks
            report = await self._verifier(
                task=task,
                result=current_output,
                success_criteria=success_criteria,
            )

            verdict = Verdict.FAIL
            feedback = ""
            checks_text = str(report)

            if isinstance(report, dict):
                raw = report.get("verdict", report.get("action", ""))
                if raw in ("PASS", "ACCEPT"):
                    verdict = Verdict.PASS
                feedback = report.get("feedback", "")
                checks_text = report.get("to_text", report.get("report", str(report)))
            else:
                raw = getattr(report, "verdict", None)
                if raw == Verdict.PASS:
                    verdict = Verdict.PASS
                elif isinstance(raw, str) and raw in ("PASS", "ACCEPT"):
                    verdict = Verdict.PASS
                feedback = getattr(report, "feedback", "")
                checks_text = getattr(report, "to_text", lambda: str(report))() if callable(getattr(report, "to_text", None)) else str(report)

            log_entry = {
                "iteration": i,
                "verdict": verdict.value,
                "feedback": feedback,
                "output_preview": current_output[:200],
            }
            iteration_logs.append(log_entry)

            # Step 3: Check result
            if verdict == Verdict.PASS:
                logger.info(f"PV Loop PASS at iteration {i}")
                return LoopResult(
                    passed=True,
                    iterations=i,
                    final_output=current_output,
                    report=checks_text,
                    iteration_logs=iteration_logs,
                )

            # Step 4: If FAIL, inject feedback for next iteration
            logger.info(f"PV Loop iteration {i} FAIL: {feedback[:100]}")

        # Exhausted iterations
        logger.warning(f"PV Loop FAIL after {self._max_iterations} iterations")
        return LoopResult(
            passed=False,
            iterations=self._max_iterations,
            final_output=current_output,
            report="Max iterations reached without passing verification.",
            iteration_logs=iteration_logs,
        )
