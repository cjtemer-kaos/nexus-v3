"""Trajectory — Session + Tool timeline (Hermes trajectory.py).

Append-only log of role/tool_call/tool_result steps for a single turn/esession.
Sanitizes surrogates, repairs tool args, provides dict serialization for persistence.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any


def _sanitize_text(s: str) -> str:
    """Remove lone surrogates and null bytes (Hermes sanitization)."""
    if not s:
        return s
    # Encode with surrogatepass then decode with replacement
    try:
        return s.encode("utf-8", "surrogatepass").decode("utf-8", "replace").replace("\x00", "")
    except Exception:
        return "".join(ch for ch in s if ch != "\x00")


# Versión del schema unificado LoopStep <-> TrajectoryStep <-> TrajectoryCompressor.
# Un solo schema: los tres comparten estos nombres de campo/evento.
TRAJECTORY_SCHEMA_VERSION = 1

#: Orden canónico de eventos por turno (verificado en tests).
TURN_EVENT_ORDER = ("turn/start", "step/start", "assistant", "tool", "step/end", "turn/end")

#: Eventos que cierran un turno abierto al reanudar (resume).
TURN_CLOSERS = ("step/end", "turn/end")


def _now_ms() -> float:
    return time.time() * 1000.0


@dataclass
class TrajectoryStep:
    iteration: int
    role: str  # user|assistant|tool|event
    content: str = ""
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    success: bool | None = None
    timestamp: float = field(default_factory=time.time)
    # --- PASO 1: campos nuevos (todos con default para compatibilidad) ---
    turn: int = 0
    step: str = ""  # kind de evento: turn/start|step/start|assistant|tool|step/end|turn/end|...
    seq: int = 0  # secuencia global asignada por Trajectory.append
    time_ms: float = 0.0  # wall-clock ms del evento
    duration_ms: float = 0.0  # duración del turno/paso (>=0)
    tokens: int = 0
    model: str = ""
    end_reason: str = ""  # stop|tool_calls|max_tokens|resume-close|budget|error|...

    def to_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "role": self.role,
            "content": _sanitize_text(self.content),
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "tool_args": self.tool_args,
            "tool_result": _sanitize_text(self.tool_result or "") if self.tool_result is not None else None,
            "success": self.success,
            "timestamp": self.timestamp,
            "turn": self.turn,
            "step": self.step,
            "seq": self.seq,
            "time_ms": self.time_ms,
            "duration_ms": max(0.0, float(self.duration_ms or 0.0)),
            "tokens": self.tokens,
            "model": self.model,
            "end_reason": self.end_reason,
            "schema_version": TRAJECTORY_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrajectoryStep":
        """Round-trip del schema unificado (ignora claves desconocidas)."""
        known = (
            "iteration", "role", "content", "tool_name", "tool_call_id",
            "tool_args", "tool_result", "success", "timestamp", "turn",
            "step", "seq", "time_ms", "duration_ms", "tokens", "model",
            "end_reason",
        )
        kw = {k: d.get(k) for k in known if k in d}
        if "iteration" not in kw:
            kw["iteration"] = 0
        if "role" not in kw:
            kw["role"] = "event"
        s = cls(**kw)  # type: ignore[arg-type]
        s.duration_ms = max(0.0, float(s.duration_ms or 0.0))
        return s

    def to_loop_step(self) -> dict:
        """Proyección al schema unificado LoopStep (dict)."""
        return {
            "iteration": self.iteration,
            "turn": self.turn,
            "seq": self.seq,
            "phase": self.step or self.role,
            "content": self.content,
            "tool_used": self.tool_name,
            "tokens": self.tokens,
            "model": self.model,
            "duration_ms": max(0.0, float(self.duration_ms or 0.0)),
            "end_reason": self.end_reason,
        }


@dataclass
class Trajectory:
    session_id: str
    task: str = ""
    steps: list[TrajectoryStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def append(self, step: TrajectoryStep) -> None:
        # seq global monótono + time_ms por defecto + clamp duration>=0
        step.seq = len(self.steps)
        if not step.time_ms:
            step.time_ms = _now_ms()
        if step.duration_ms is None or step.duration_ms < 0:  # type: ignore[operator]
            step.duration_ms = 0.0
        if not step.turn and step.iteration:
            step.turn = step.iteration
        self.steps.append(step)

    def append_event(self, kind: str, content: str = "", iteration: int = 0, turn: int = 0, **kw: Any) -> TrajectoryStep:
        """Evento de ciclo de vida (turn/start, step/start, step/end, turn/end...)."""
        s = TrajectoryStep(
            iteration=iteration or turn,
            role="event",
            content=content,
            turn=turn or iteration,
            step=kind,
            duration_ms=max(0.0, float(kw.pop("duration_ms", 0.0) or 0.0)),
            **kw,
        )
        self.append(s)
        return s

    def close_interrupted_turns(self, reason: str = "resume-close") -> list[TrajectoryStep]:
        """Inserta closers (step/end + turn/end) por cada turno sin cerrar.

        Resume: un turno queda abierto si hay turn/start sin su turn/end.
        No duplica closers si el turno ya está cerrado.
        """
        closers: list[TrajectoryStep] = []
        open_turns: dict[int, bool] = {}
        for st in self.steps:
            if st.step == "turn/start":
                open_turns[st.turn or st.iteration] = True
            elif st.step == "turn/end":
                open_turns.pop(st.turn or st.iteration, None)
        for t in sorted(open_turns):
            closers.append(self.append_event("step/end", content=f"[resume] cierre de turno {t}", iteration=t, turn=t, end_reason=reason))
            closers.append(self.append_event("turn/end", content=f"[resume] cierre de turno {t}", iteration=t, turn=t, end_reason=reason))
        return closers

    def append_user(self, content: str, iteration: int = 0, **kw: Any) -> TrajectoryStep:
        s = TrajectoryStep(iteration=iteration, role="user", content=content, turn=kw.pop("turn", iteration), step=kw.pop("step", ""), **kw)
        self.append(s)
        return s

    def append_assistant(self, content: str, iteration: int, tool_name: str | None = None, tool_call_id: str | None = None, tool_args: dict | None = None, **kw: Any) -> TrajectoryStep:
        s = TrajectoryStep(iteration=iteration, role="assistant", content=content, tool_name=tool_name, tool_call_id=tool_call_id, tool_args=tool_args, turn=kw.pop("turn", iteration), step=kw.pop("step", "assistant"), **kw)
        self.append(s)
        return s

    def append_tool_result(self, tool_name: str, result: str, success: bool, iteration: int, tool_call_id: str | None = None, **kw: Any) -> TrajectoryStep:
        s = TrajectoryStep(iteration=iteration, role="tool", content=result, tool_name=tool_name, tool_call_id=tool_call_id, tool_result=result, success=success, turn=kw.pop("turn", iteration), step=kw.pop("step", "tool"), **kw)
        self.append(s)
        return s

    def to_messages(self) -> list[dict]:
        """Convert to OpenAI-style messages for LLM context."""
        msgs: list[dict] = []
        for st in self.steps:
            if st.role == "user":
                msgs.append({"role": "user", "content": st.content})
            elif st.role == "assistant":
                if st.tool_name:
                    msgs.append({
                        "role": "assistant",
                        "content": st.content or None,
                        "tool_calls": [{"id": st.tool_call_id or f"call_{st.iteration}", "type": "function", "function": {"name": st.tool_name, "arguments": json.dumps(st.tool_args or {}, ensure_ascii=False)}}],
                    })
                else:
                    msgs.append({"role": "assistant", "content": st.content})
            elif st.role == "tool":
                msgs.append({"role": "tool", "tool_call_id": st.tool_call_id or f"call_{st.iteration}", "content": st.tool_result or st.content})
        return msgs

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "task": self.task, "created_at": self.created_at, "steps": [s.to_dict() for s in self.steps]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @property
    def last_assistant(self) -> TrajectoryStep | None:
        for s in reversed(self.steps):
            if s.role == "assistant":
                return s
        return None

    def __len__(self) -> int:
        return len(self.steps)
