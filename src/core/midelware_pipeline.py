import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List

logger = logging.getLogger("nexus-middleware")


class MiddlewareAction(Enum):
    CONTINUE = "continue"
    STOP = "stop"
    COMPACT = "compact"
    INJECT = "inject"


class MiddlewarePhase(Enum):
    PRE_EXECUTE = "pre_execute"
    POST_EXECUTE = "post_execute"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    PRE_TOOL_RESULT = "pre_tool_result"
    POST_TOOL_RESULT = "post_tool_result"
    ON_ERROR = "on_error"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    ON_CONFIG_CHANGE = "on_config_change"
    MEMORY_WRITE = "memory_write"
    PRE_MEMORY_WRITE = "pre_memory_write"
    POST_MEMORY_WRITE = "post_memory_write"
    ON_COMPACT = "on_compact"
    ON_INPUT = "on_input"
    ON_MEMORY = "on_memory"
    ON_CONTEXT_BUILD = "on_context_build"
    PRE_CONTEXT_BUILD = "pre_context_build"
    POST_CONTEXT_BUILD = "post_context_build"
    ON_INVOKE_LLM = "on_invoke_llm"
    ON_LLM_RESPONSE = "on_llm_response"
    ON_OUTPUT = "on_output"
    PRE_AGENT_SPAWN = "pre_agent_spawn"
    POST_AGENT_SPAWN = "post_agent_spawn"
    PRE_AGENT_JOIN = "pre_agent_join"
    POST_AGENT_JOIN = "post_agent_join"
    ON_TASK_COMPLETE = "on_task_complete"
    ON_TASK_FAIL = "on_task_fail"
    ON_HANDOFF = "on_handoff"
    ON_SKILL_LOAD = "on_skill_load"
    ON_MODEL_SELECT = "on_model_select"
    ON_CACHE_HIT = "on_cache_hit"
    ON_CACHE_MISS = "on_cache_miss"
    LEARN = "learn"


@dataclass
class MiddlewareResult:
    action: MiddlewareAction = MiddlewareAction.CONTINUE
    modified_data: Any = None
    message: str = ""
    injection_data: Any = None


class BaseMiddleware:
    def __init__(self, name: str, phase: MiddlewarePhase, priority: int = 0):
        self.name = name
        self.phase = phase
        self.priority = priority
        self.call_count = 0
        self.total_time = 0.0
        self.error_count = 0

    async def handle(self, context: Dict) -> MiddlewareResult:
        raise NotImplementedError

    @property
    def avg_time_ms(self) -> float:
        if self.call_count == 0:
            return 0.0
        return (self.total_time / self.call_count) * 1000


class MiddlewarePipeline:
    def __init__(self, pipeline_id: str = "global"):
        self._pipeline_id = pipeline_id
        self._middleware: Dict[MiddlewarePhase, List[BaseMiddleware]] = {
            phase: [] for phase in MiddlewarePhase
        }
        self._stats = {
            "total_executions": 0,
            "total_errors": 0,
            "total_time": 0.0,
            "stops": 0,
            "compactions": 0,
            "injections": 0,
        }

    def register(self, mw: BaseMiddleware):
        self._middleware[mw.phase].append(mw)
        self._middleware[mw.phase].sort(key=lambda m: m.priority, reverse=True)

    async def execute(self, phase: MiddlewarePhase, context: Dict) -> MiddlewareResult:
        start = time.time()
        combined = MiddlewareResult()

        for mw in self._middleware[phase]:
            try:
                mw_start = time.time()
                result = await mw.handle(context)
                mw.total_time += time.time() - mw_start
                mw.call_count += 1

                if result.action == MiddlewareAction.STOP:
                    self._stats["stops"] += 1
                    return result

                if result.action == MiddlewareAction.COMPACT:
                    self._stats["compactions"] += 1
                    if result.modified_data is not None:
                        combined.modified_data = result.modified_data
                    continue

                if result.action == MiddlewareAction.INJECT:
                    self._stats["injections"] += 1
                    combined.injection_data = result.injection_data
                    continue

                if result.modified_data is not None:
                    combined.modified_data = result.modified_data
                if result.message:
                    combined.message = result.message

            except Exception:
                mw.error_count += 1
                self._stats["total_errors"] += 1

        self._stats["total_executions"] += 1
        self._stats["total_time"] += time.time() - start
        return combined

    def get_stats(self) -> Dict:
        mw_stats = {}
        for phase, middlewares in self._middleware.items():
            for mw in middlewares:
                mw_stats[mw.name] = {
                    "phase": mw.phase.value,
                    "priority": mw.priority,
                    "calls": mw.call_count,
                    "errors": mw.error_count,
                    "avg_time_ms": round(mw.avg_time_ms, 2),
                }
        return {**self._stats, "middleware": mw_stats}

    def clear(self):
        self._middleware = {phase: [] for phase in MiddlewarePhase}
        self._stats = {
            "total_executions": 0,
            "total_errors": 0,
            "total_time": 0.0,
            "stops": 0,
            "compactions": 0,
            "injections": 0,
        }


global_pipeline = MiddlewarePipeline("global")
