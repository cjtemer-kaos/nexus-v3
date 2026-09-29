"""
ExtensionPipeline - 8 hooks lifecycle para gems de SuperNEXUS

Cada gema intercepta/transforma en 8 puntos del ciclo de vida:
1. on_input - Intercepta input del usuario antes de procesamiento
2. on_memory - Intercepta acceso a memoria
3. on_context_build - Intercepta construccion de contexto
4. on_invoke_llm - Intercepta llamada al LLM
5. on_llm_response - Intercepta respuesta del LLM
6. on_tool_call - Intercepta llamada a herramientas
7. on_tool_result - Intercepta resultado de herramientas
8. on_output - Intercepta output final al usuario

Permite:
- Logging y auditoria de cada paso
- Transformacion de datos (compresion, sanitizacion)
- Inyeccion de contexto adicional
- Validacion de seguridad
- Metricas de rendimiento
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


class HookPhase(Enum):
    """Fases del pipeline de extension"""
    ON_INPUT = "on_input"
    ON_MEMORY = "on_memory"
    ON_CONTEXT_BUILD = "on_context_build"
    ON_INVOKE_LLM = "on_invoke_llm"
    ON_LLM_RESPONSE = "on_llm_response"
    ON_TOOL_CALL = "on_tool_call"
    ON_TOOL_RESULT = "on_tool_result"
    ON_OUTPUT = "on_output"


@dataclass
class HookContext:
    """Contexto compartido entre hooks"""
    phase: HookPhase
    gem_id: str
    data: Any
    metadata: Dict[str, Any] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)
    should_skip: bool = False
    should_abort: bool = False
    abort_reason: str = ""


class ExtensionHook:
    """Hook individual que se ejecuta en una fase especifica"""

    def __init__(
        self,
        phase: HookPhase,
        name: str,
        handler: Callable[[HookContext], HookContext],
        priority: int = 0,  # Mayor = primero
        enabled: bool = True,
    ):
        self.phase = phase
        self.name = name
        self.handler = handler
        self.priority = priority
        self.enabled = enabled
        self.call_count = 0
        self.total_time = 0.0
        self.error_count = 0

    def execute(self, context: HookContext) -> HookContext:
        """Ejecuta el hook"""
        if not self.enabled:
            return context

        start = time.time()
        try:
            result = self.handler(context)
            self.call_count += 1
            self.total_time += time.time() - start
            return result or context
        except Exception as e:
            self.error_count += 1
            logger.error(f"Hook {self.name} error: {e}")
            context.metadata[f"hook_error_{self.name}"] = str(e)
            return context

    @property
    def avg_time(self) -> float:
        if self.call_count == 0:
            return 0.0
        return self.total_time / self.call_count


class ExtensionPipeline:
    """Pipeline de 8 hooks para gems de SuperNEXUS"""

    def __init__(self, gem_id: str = "global"):
        self.gem_id = gem_id
        self._hooks: Dict[HookPhase, List[ExtensionHook]] = {
            phase: [] for phase in HookPhase
        }
        self._stats = {
            "total_executions": 0,
            "total_errors": 0,
            "total_time": 0.0,
        }

    def register_hook(
        self,
        phase: HookPhase,
        name: str,
        handler: Callable[[HookContext], HookContext],
        priority: int = 0,
        enabled: bool = True,
    ):
        """Registra un hook en una fase"""
        hook = ExtensionHook(phase, name, handler, priority, enabled)
        self._hooks[phase].append(hook)
        # Ordenar por prioridad (mayor primero)
        self._hooks[phase].sort(key=lambda h: h.priority, reverse=True)
        logger.debug(f"Hook registrado: {name} en {phase.value} (priority={priority})")

    def execute_phase(self, phase: HookPhase, data: Any, metadata: Dict = None) -> HookContext:
        """Ejecuta todos los hooks de una fase"""
        context = HookContext(
            phase=phase,
            gem_id=self.gem_id,
            data=data,
            metadata=metadata or {},
        )

        for hook in self._hooks[phase]:
            if context.should_abort:
                break
            context = hook.execute(context)

        self._stats["total_executions"] += 1
        return context

    # Metodos de conveniencia para cada fase
    def on_input(self, user_input: str, metadata: Dict = None) -> HookContext:
        """Fase 1: Intercepta input del usuario"""
        return self.execute_phase(HookPhase.ON_INPUT, user_input, metadata)

    def on_memory(self, query: str, metadata: Dict = None) -> HookContext:
        """Fase 2: Intercepta acceso a memoria"""
        return self.execute_phase(HookPhase.ON_MEMORY, query, metadata)

    def on_context_build(self, context_data: Dict, metadata: Dict = None) -> HookContext:
        """Fase 3: Intercepta construccion de contexto"""
        return self.execute_phase(HookPhase.ON_CONTEXT_BUILD, context_data, metadata)

    def on_invoke_llm(self, prompt: str, metadata: Dict = None) -> HookContext:
        """Fase 4: Intercepta llamada al LLM"""
        return self.execute_phase(HookPhase.ON_INVOKE_LLM, prompt, metadata)

    def on_llm_response(self, response: str, metadata: Dict = None) -> HookContext:
        """Fase 5: Intercepta respuesta del LLM"""
        return self.execute_phase(HookPhase.ON_LLM_RESPONSE, response, metadata)

    def on_tool_call(self, tool_name: str, params: Dict, metadata: Dict = None) -> HookContext:
        """Fase 6: Intercepta llamada a herramientas"""
        return self.execute_phase(HookPhase.ON_TOOL_CALL, {"tool": tool_name, "params": params}, metadata)

    def on_tool_result(self, result: Any, metadata: Dict = None) -> HookContext:
        """Fase 7: Intercepta resultado de herramientas"""
        return self.execute_phase(HookPhase.ON_TOOL_RESULT, result, metadata)

    def on_output(self, output: str, metadata: Dict = None) -> HookContext:
        """Fase 8: Intercepta output final"""
        return self.execute_phase(HookPhase.ON_OUTPUT, output, metadata)

    def get_stats(self) -> Dict:
        """Estadisticas del pipeline"""
        hook_stats = {}
        for phase, hooks in self._hooks.items():
            for hook in hooks:
                hook_stats[f"{hook.name}"] = {
                    "phase": hook.phase.value,
                    "calls": hook.call_count,
                    "errors": hook.error_count,
                    "avg_time_ms": round(hook.avg_time * 1000, 2),
                }

        return {
            **self._stats,
            "hooks": hook_stats,
        }

    def disable_hook(self, name: str):
        """Desactiva un hook por nombre"""
        for hooks in self._hooks.values():
            for hook in hooks:
                if hook.name == name:
                    hook.enabled = False
                    logger.info(f"Hook desactivado: {name}")
                    return

    def enable_hook(self, name: str):
        """Activa un hook por nombre"""
        for hooks in self._hooks.values():
            for hook in hooks:
                if hook.name == name:
                    hook.enabled = True
                    logger.info(f"Hook activado: {name}")
                    return


# Pipeline global compartido
global_pipeline = ExtensionPipeline("global")
