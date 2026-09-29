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


@dataclass
class TrajectoryStep:
    iteration: int
    role: str  # user|assistant|tool
    content: str = ""
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    success: bool | None = None
    timestamp: float = field(default_factory=time.time)

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
        }


@dataclass
class Trajectory:
    session_id: str
    task: str = ""
    steps: list[TrajectoryStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def append(self, step: TrajectoryStep) -> None:
        self.steps.append(step)

    def append_user(self, content: str, iteration: int = 0) -> TrajectoryStep:
        s = TrajectoryStep(iteration=iteration, role="user", content=content)
        self.append(s)
        return s

    def append_assistant(self, content: str, iteration: int, tool_name: str | None = None, tool_call_id: str | None = None, tool_args: dict | None = None) -> TrajectoryStep:
        s = TrajectoryStep(iteration=iteration, role="assistant", content=content, tool_name=tool_name, tool_call_id=tool_call_id, tool_args=tool_args)
        self.append(s)
        return s

    def append_tool_result(self, tool_name: str, result: str, success: bool, iteration: int, tool_call_id: str | None = None) -> TrajectoryStep:
        s = TrajectoryStep(iteration=iteration, role="tool", content=result, tool_name=tool_name, tool_call_id=tool_call_id, tool_result=result, success=success)
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
