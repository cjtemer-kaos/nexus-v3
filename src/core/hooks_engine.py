"""Hooks Engine — middleware (Hermes/Cordis style).

Phases: pre_tool_use, post_tool_use, on_error, on_compact, session_start, session_end, on_context_build
3-gate security: scope + prompt_security + content filter (optional)

Design: lightweight event emitter with ordered handlers. Each handler returns
CONTINUE | STOP | COMPACT | INJECT semantics (Cordis).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from enum import Enum

logger = logging.getLogger("nexus-hooks")


class HookAction(str, Enum):
    CONTINUE = "continue"
    STOP = "stop"
    COMPACT = "compact"
    INJECT = "inject"


@dataclass
class HookResult:
    action: HookAction = HookAction.CONTINUE
    message: str = ""
    inject_content: str = ""  # for INJECT action
    metadata: dict = field(default_factory=dict)



# --- compat shim para imports legacy de director/conductor ---
class HookPhase(str, Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    PRE_TOOL_RESULT = "pre_tool_result"
    POST_TOOL_RESULT = "post_tool_result"
    PRE_EXECUTE = "pre_execute"
    POST_EXECUTE = "post_execute"
    ON_ERROR = "on_error"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    ON_CONFIG_CHANGE = "on_config_change"
    MEMORY_WRITE = "memory_write"
    PRE_MEMORY_WRITE = "pre_memory_write"
    POST_MEMORY_WRITE = "post_memory_write"
    ON_COMPACT = "on_compact"
    ON_INPUT = "on_input"
    ON_OUTPUT = "on_output"
    ON_CONTEXT_BUILD = "on_context_build"
    ON_INVOKE_LLM = "on_invoke_llm"
    ON_LLM_RESPONSE = "on_llm_response"
    LEARN = "learn"

HookHandler = Callable[[str, dict], HookResult | Awaitable[HookResult] | None]


class HooksEngine:
    """Ordered hook dispatcher."""

    VALID_PHASES = frozenset({
        "pre_tool_use", "post_tool_use", "on_error", "on_compact",
        "session_start", "session_end", "on_context_build", "pre_llm_call", "post_llm_call",
    })

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.security_gate: bool = bool(self.config.get("security_gate", True))
        self._handlers: dict[str, list[HookHandler]] = {p: [] for p in self.VALID_PHASES}
        self._phases_enabled: set[str] = set(self.config.get("phases", list(self.VALID_PHASES)))

    def on(self, phase: str, handler: HookHandler) -> None:
        if phase not in self.VALID_PHASES:
            logger.warning(f"fase hook desconocida: {phase}")
            # still register for extensibility
            self._handlers.setdefault(phase, [])
        self._handlers.setdefault(phase, []).append(handler)

    def off(self, phase: str, handler: HookHandler) -> None:
        handlers = self._handlers.get(phase, [])
        try:
            handlers.remove(handler)
        except ValueError:
            pass

    async def emit(self, phase: str, payload: dict[str, Any]) -> HookResult:
        """Emit to all handlers in order. Short-circuits on STOP/COMPACT."""
        handlers = self._handlers.get(phase, [])
        if not handlers:
            return HookResult(action=HookAction.CONTINUE)
        last = HookResult(action=HookAction.CONTINUE)
        for h in handlers:
            try:
                if inspect.iscoroutinefunction(h):
                    res = await h(phase, payload)
                else:
                    res = h(phase, payload)  # type: ignore
                    if asyncio.isfuture(res) or asyncio.iscoroutine(res):
                        res = await res  # type: ignore
                if res is None:
                    continue
                if isinstance(res, dict):
                    # allow dict shorthand
                    res = HookResult(
                        action=HookAction(res.get("action", "continue")),
                        message=res.get("message", ""),
                        inject_content=res.get("inject_content", ""),
                        metadata=res.get("metadata", {}),
                    )
                last = res
                if res.action in (HookAction.STOP, HookAction.COMPACT):
                    break
            except Exception as e:
                logger.error(f"error manejador hook en {phase}: {e}")
                continue
        return last

    def emit_sync(self, phase: str, payload: dict[str, Any]) -> HookResult:
        """Sync wrapper for non-async contexts.

        Dentro de un loop ya corriendo no se pueden await-ear los handlers async.
        Se saltean, pero avisando: un hook de STOP/COMPACT que desaparece en
        silencio es peor que un error visible.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop — run new loop
            return asyncio.run(self.emit(phase, payload))
        # Already in loop — caller should await; fallback to sync iteration without await
        last = HookResult(action=HookAction.CONTINUE)
        salteados = 0
        for h in self._handlers.get(phase, []):
            if inspect.iscoroutinefunction(h):
                salteados += 1
                continue  # skip async handlers in sync context
            try:
                res = h(phase, payload)  # type: ignore
                if isinstance(res, HookResult) and res.action in (HookAction.STOP, HookAction.COMPACT):
                    return res
                if res is not None:
                    last = res  # type: ignore
            except Exception as e:
                logger.error(f"error hook síncrono {phase}: {e}")
        if salteados:
            logger.warning(
                "emit_sync(%s): %d handler(s) async salteados dentro de un loop; "
                "usá emit() con await para que corran", phase, salteados)
        return last

    def has_handlers(self, phase: str) -> bool:
        return bool(self._handlers.get(phase))

    def handler_count(self, phase: str | None = None) -> int:
        if phase:
            return len(self._handlers.get(phase, []))
        return sum(len(v) for v in self._handlers.values())

    def stats(self) -> dict:
        return {
            "security_gate": self.security_gate,
            "phases_enabled": sorted(self._phases_enabled),
            "handlers": {k: len(v) for k, v in self._handlers.items()},
            "total_handlers": self.handler_count(),
            "file_hooks": int(getattr(self, "file_hooks_count", 0) or 0),
            "file_hooks_dir": str(getattr(self, "file_hooks_dir", "") or ""),
        }
