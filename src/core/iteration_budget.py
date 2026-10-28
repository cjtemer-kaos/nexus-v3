"""Iteration Budget — corta loops infinitos (Hermes iteration_budget.py).

Tracks iterations, tokens, wall-clock time, and tool calls per turn.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class IterationBudget:
    """Presupuesto estilo Hermes: max_iterations, max_tokens, max_wall_ms, max_tool_calls."""

    max_iterations: int = 25
    max_tokens: int = 32000  # alineado con la compactacion (32000*0.75) para no cortar antes
    max_wall_ms: float = 300_000  # 5 min per turn
    max_tool_calls: int = 50
    # Guardia de fuga: tokens TOTALES del run (prompt reenviado + completion).
    # `max_tokens` cuenta solo la generacion (contar el prompt ahi cortaba el run
    # en 2-4 turnos); esto es aparte y solo frena un loop patologico. 0 = sin tope.
    max_prompt_tokens: int = 0

    # runtime counters
    iterations: int = 0
    tokens: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def tick(self, tokens: int = 0, tool_calls: int = 0) -> bool:
        """Advance one iteration. Returns True if budget still available."""
        self.iterations += 1
        self.tokens += tokens
        self.tool_calls += tool_calls
        return not self.exhausted

    def add_tokens(self, n: int) -> None:
        self.tokens += n

    def add_prompt_tokens(self, n: int) -> None:
        self.prompt_tokens += max(0, int(n))

    def add_tool_calls(self, n: int = 1) -> None:
        self.tool_calls += n

    @property
    def wall_ms(self) -> float:
        return (time.monotonic() - self.started_at) * 1000

    @property
    def exhausted(self) -> bool:
        if self.iterations >= self.max_iterations:
            return True
        if self.tokens >= self.max_tokens:
            return True
        if self.max_prompt_tokens and self.prompt_tokens >= self.max_prompt_tokens:
            return True
        if self.tool_calls >= self.max_tool_calls:
            return True
        if self.wall_ms >= self.max_wall_ms:
            return True
        return False

    @property
    def reason(self) -> str | None:
        if self.iterations >= self.max_iterations:
            return f"máx_iteraciones {self.max_iterations} alcanzado"
        if self.tokens >= self.max_tokens:
            return f"máx_tokens {self.max_tokens} alcanzado"
        if self.max_prompt_tokens and self.prompt_tokens >= self.max_prompt_tokens:
            return (f"máx_tokens_del_run {self.max_prompt_tokens} alcanzado "
                    f"({self.prompt_tokens} enviados)")
        if self.tool_calls >= self.max_tool_calls:
            return f"máx_llamadas_herramienta {self.max_tool_calls} alcanzado"
        if self.wall_ms >= self.max_wall_ms:
            return f"máx_tiempo_pared {self.max_wall_ms} alcanzado"
        return None

    @property
    def remaining_iterations(self) -> int:
        return max(0, self.max_iterations - self.iterations)

    def to_dict(self) -> dict:
        return {
            "max_iterations": self.max_iterations,
            "max_tokens": self.max_tokens,
            "max_wall_ms": self.max_wall_ms,
            "max_tool_calls": self.max_tool_calls,
            "max_prompt_tokens": self.max_prompt_tokens,
            "iterations": self.iterations,
            "tokens": self.tokens,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "wall_ms": round(self.wall_ms, 1),
            "exhausted": self.exhausted,
            "reason": self.reason,
        }

    def reset(self) -> None:
        self.iterations = 0
        self.tokens = 0
        self.tool_calls = 0
        self.prompt_tokens = 0
        self.started_at = time.monotonic()
