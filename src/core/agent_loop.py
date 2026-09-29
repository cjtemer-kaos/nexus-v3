"""Bucle Agente — TDAO (Think-Decide-Act-Observe) + budget + error classifier + heartbeat.

Origen: SuperNEXUS agent_loop.py (TDAO + ErrorAction) + Hermes conversation_loop (3900 LOC) + iteration_budget + error_classifier + cordis hooks.
Ver BLUEPRINT.md §6.

Flow per iteration:
  1. THINK  — LLM call via ProviderRouter (with fallback)
  2. DECIDE — parse tool_calls vs respuesta final (content)
  3. ACT    — scope.guard + hooks.pre_tool_use + tool_registry.dispatch + hooks.post_tool_use
  4. OBSERVE — append to SessionLog + EventStore + Trajectory, classify error, check budget, maybe compact

Integration:
  - SessionLog (Fase 0) for persistent message history
  - EventStore for replay/audit
  - CompactionPipeline for context window management
  - HooksEngine for middleware
  - ToolRegistry for unified dispatch
"""
from __future__ import annotations

import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .provider_base import LLMMessage, ToolCall
from .iteration_budget import IterationBudget
from .error_classifier import ErrorAction, classify_error
from .trajectory import Trajectory, TrajectoryStep

logger = logging.getLogger("nexus-agent-loop")

# Re-export for callers
__all__ = ["ErrorAction", "ToolResult", "LoopStep", "LoopResult", "IterationBudget", "AgentLoop"]


@dataclass(frozen=True, slots=True)
class ToolResult:
    success: bool
    output: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoopStep:
    iteration: int
    phase: str  # think|decide|act|observe
    content: str
    tool_used: str | None = None
    result: ToolResult | None = None


@dataclass
class LoopResult:
    task: str
    success: bool
    final_output: str
    steps: list[LoopStep]
    iterations: int
    total_tokens: int = 0
    duration_ms: float = 0
    trajectory: Trajectory | None = None
    compact_metrics: Any | None = None

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "success": self.success,
            "final_output": self.final_output,
            "iterations": self.iterations,
            "total_tokens": self.total_tokens,
            "duration_ms": round(self.duration_ms, 1),
            "steps": [
                {"iteration": s.iteration, "phase": s.phase, "content": s.content[:2000], "tool_used": s.tool_used, "result": s.result.output[:2000] if s.result else None}
                for s in self.steps
            ],
        }


class AgentLoop:
    """TDAO loop. ProviderRouter + ToolRegistry + Scope + Hooks + SessionLog + EventStore."""

    def __init__(
        self,
        provider_router=None,
        tool_registry=None,
        scope=None,
        hooks=None,
        budget: IterationBudget | None = None,
        session_log=None,
        event_store=None,
        compaction=None,
        max_iterations: int = 25,
        heartbeat_ms: int = 2000,
    ):
        self.router = provider_router
        self.tools = tool_registry
        self.scope = scope
        self.hooks = hooks
        self.budget = budget or IterationBudget(max_iterations=max_iterations)
        self.session_log = session_log
        self.event_store = event_store
        self.compaction = compaction
        self.heartbeat_ms = heartbeat_ms

        # Per-task retry/replan counters
        self._retry_counts: dict[str, int] = {}
        self._replan_counts: dict[str, int] = {}

    def _extract_text_tool_calls(self, content: str) -> list:
        """Respaldo para modelos que escriben JSON en texto en vez de tool_calls.

        Acepta {"tool": "<nombre>", "arguments": {...}} o
        {"tool": "<nombre>", "parameters": {...}} o el resto de claves como args.
        Solo acepta herramientas registradas (self.tools) para no ejecutar basura.
        """
        out: list = []
        if not content or "{" not in content:
            return out
        try:
            names = set()
            if self.tools is not None and hasattr(self.tools, "list_tools"):
                try:
                    names = {t.get("name", "") for t in self.tools.list_tools()}
                except Exception:
                    names = set()
        except Exception:
            names = set()
        # 0) Sintaxis tipo llamada: nombre("texto") o nombre('texto') o nombre(args)
        # Acepta prefijo tool_ (tool_echo -> echo) si el resto coincide con registrada.
        import re as _re2
        schemas: dict[str, dict] = {}
        if self.tools is not None and hasattr(self.tools, "_tools"):
            try:
                for _tn, _te in self.tools._tools.items():
                    _p = getattr(_te, "parameters", {}) or {}
                    schemas[_tn] = _p
            except Exception:
                pass
        for _m in _re2.finditer(r"(?i)(?:^|[\s:>`\"'(\[])([a-zA-Z_][a-zA-Z0-9_\.]*)\(\s*\"([^\"]{1,500})\"\s*\)", content):
            _raw = _m.group(1)
            _txt = _m.group(2)
            _cand = _raw[5:] if _raw.lower().startswith("tool_") else _raw
            _match = None
            if _cand in names or _cand in schemas:
                _match = _cand
            else:
                for _n in list(names) + list(schemas.keys()):
                    if _n and _n.lower() == _cand.lower():
                        _match = _n
                        break
            if not _match:
                continue
            _props = {}
            try:
                _props = (schemas.get(_match, {}) or {}).get("properties", {}) or {}
            except Exception:
                _props = {}
            _argname = next(iter(_props.keys()), "text")
            try:
                out.append(ToolCall(id=f"text-{len(out)}", name=_match, arguments={_argname: _txt}))
            except Exception:
                continue
            if len(out) >= 5:
                return out
        # 1) Escáner de bloques JSON balanceados (soporta objetos anidados)
        starts = [i for i, ch in enumerate(content) if ch == "{"]
        for si in starts:
            depth = 0
            in_str = False
            esc = False
            end = -1
            for i in range(si, min(si + 4000, len(content))):
                ch = content[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end < 0:
                continue
            try:
                obj = json.loads(content[si:end])
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            name = str(obj.get("tool", ""))
            if not name:
                # Forma OpenAI suelta: {"name": "<tool>", "arguments": {...}}
                _nm = str(obj.get("name", ""))
                if _nm and ("arguments" in obj or "parameters" in obj):
                    name = _nm
            if not name:
                continue
            if names and name not in names:
                continue
            args = obj.get("arguments")
            if not isinstance(args, dict):
                args = obj.get("parameters")
            if not isinstance(args, dict):
                args = {k: v for k, v in obj.items() if k != "tool"}
            try:
                out.append(ToolCall(id=f"text-{len(out)}", name=name, arguments=args if isinstance(args, dict) else {}))
            except Exception:
                continue
            if len(out) >= 5:
                break
        return out

    async def run(self, task: str, session_id: str, hint: str | None = None, system_prompt: str | None = None, force_tools: bool = False, model: str | None = None) -> LoopResult:
        """Run TDAO loop until done, ABORT, or budget exhausted.

        Args:
            task: user task / prompt
            session_id: SessionLog session id
            hint: provider hint (simple|coding|reasoning|deep etc)
            system_prompt: optional system prompt override
        """
        t0 = time.monotonic()
        steps: list[LoopStep] = []
        trajectory = Trajectory(session_id=session_id, task=task)
        total_tokens = 0
        success = False
        final_output = ""
        self._empty_retries = 0  # type: ignore[attr-defined]
        self._req_model = model  # type: ignore[attr-defined]
        self._tool_fp_window: list[tuple[str, str]] = []  # type: ignore[attr-defined]

        # Init trajectory + session
        trajectory.append_user(task, iteration=0)
        if self.session_log is not None:
            try:
                from .session_log import Message
                self.session_log.append(session_id, Message(role="user", content=task))
            except Exception as e:
                logger.debug(f"session append user falló: {e}")
        if self.event_store is not None:
            try:
                self.event_store.append_event("user_message", {"content": task}, session_id=session_id)
            except Exception:
                pass
        if self.hooks is not None:
            try:
                await self.hooks.emit("session_start", {"session_id": session_id, "task": task, "hint": hint})
            except Exception:
                pass

        # Build initial messages
        messages: list[LLMMessage] = []
        if system_prompt:
            messages.append(LLMMessage(role="system", content=system_prompt))
        else:
            messages.append(LLMMessage(role="system", content="Eres Nexus v3, un agente con herramientas reales. Usa tool_calls SOLO si la tarea requiere acceder a archivos, sistema o ejecutar algo. Para tareas de generación (diseño, textos, código, creatividad) responde directamente con el contenido. No invoques herramientas innecesarias. Sé conciso."))

        # Load history from session_log if available
        history_limit = 20
        if self.session_log is not None:
            try:
                from .session_log import Message as SMsg  # type: ignore
                recent = self.session_log.get_messages(session_id, limit=history_limit)
                # Skip the just-appended user message duplicate
                for m in recent[:-1]:
                    if m.role in ("system", "user", "assistant"):
                        messages.append(LLMMessage(role=m.role, content=m.content))
                    elif m.role == "tool":
                        messages.append(LLMMessage(role="tool", content=m.content, tool_call_id=m.tool_call_id))
            except Exception as e:
                logger.debug(f"load history falló: {e}")

        # Add current task as user message (if not already via history)
        if not any(m.role == "user" and task in (m.content or "") for m in messages):
            messages.append(LLMMessage(role="user", content=task))

        # Compaction pre-check
        if self.compaction is not None:
            try:
                dict_msgs = [{"role": m.role, "content": m.content or ""} for m in messages]
                # Use router to decide if we need compaction via token estimate
                max_tokens = 32000
                if hasattr(self.compaction, "maybe_compact") and self.compaction.maybe_compact(dict_msgs, max_tokens):
                    dict_msgs, cmetrics = await self.compaction.compact(dict_msgs, int(max_tokens * 0.75))
                    messages = [LLMMessage(role=d["role"], content=d.get("content")) for d in dict_msgs]
            except Exception as e:
                logger.debug(f"compaction pre-check falló: {e}")

        # Prepare tools for LLM
        openai_tools: list[dict] | None = None
        if self.tools is not None:
            try:
                openai_tools = self.tools.to_openai_tools()
                if not openai_tools:
                    openai_tools = None
            except Exception:
                openai_tools = None

        # Main loop
        iteration = 0
        max_iters = self.budget.max_iterations
        retry_budget = getattr(self.budget, "max_iterations", 25)  # fallback
        # Config error policy
        error_retry_limit = 3
        error_replan_limit = 2
        if hasattr(self, "_error_policy"):
            error_retry_limit = self._error_policy.get("retry", 3)
            error_replan_limit = self._error_policy.get("replan", 2)

        while iteration < max_iters:
            if self.budget.exhausted:
                steps.append(LoopStep(iteration=iteration, phase="observe", content=f"presupuesto agotado: {self.budget.reason}"))
                final_output = f"[presupuesto agotado: {self.budget.reason}]"
                break

            iteration += 1
            self.budget.tick()

            # ---- THINK: LLM call ----
            steps.append(LoopStep(iteration=iteration, phase="think", content=f"pensando (iter {iteration})"))
            llm_resp = None
            try:
                if self.router is None:
                    raise RuntimeError("sin enrutador de proveedores configurado")
                # Hooks pre_llm_call
                if self.hooks is not None and self.hooks.has_handlers("pre_llm_call"):
                    await self.hooks.emit("pre_llm_call", {"iteration": iteration, "messages": len(messages), "hint": hint})
                # Router.chat with fallback
                if hasattr(self.router, "chat"):
                    llm_resp = await self.router.chat(messages, tools=openai_tools, hint=hint, force_tools=force_tools, model=getattr(self, "_req_model", None))
                else:
                    # Fallback: resolve then chat
                    routed = self.router.resolve(task_hint=hint)
                    if routed is None:
                        raise RuntimeError(f"sin proveedor para pista={hint}")
                    llm_resp = await routed.provider.chat(messages, tools=openai_tools, model=routed.model)
                # Hooks post_llm_call
                if self.hooks is not None and self.hooks.has_handlers("post_llm_call"):
                    await self.hooks.emit("post_llm_call", {"iteration": iteration, "finish_reason": llm_resp.finish_reason, "has_tool_calls": llm_resp.has_tool_calls})
            except Exception as e:
                err_str = str(e)
                steps.append(LoopStep(iteration=iteration, phase="observe", content=f"Error LLM: {err_str}"))
                # Check for context window error -> reactive compact
                if "context" in err_str.lower() or "too long" in err_str.lower() or "limit" in err_str.lower():
                    if self.compaction is not None:
                        try:
                            dict_msgs = [{"role": m.role, "content": m.content or ""} for m in messages]
                            dict_msgs = await self.compaction.reactive_compact(dict_msgs, err_str)
                            messages = [LLMMessage(role=d["role"], content=d.get("content")) for d in dict_msgs]
                            continue  # retry LLM call with compacted context
                        except Exception:
                            pass
                final_output = f"[Error LLM: {err_str}]"
                break

            if llm_resp is None:
                final_output = "[sin respuesta LLM]"
                break

            # Track tokens
            usage = getattr(llm_resp, "usage", {}) or {}
            turn_tokens = int(usage.get("total_tokens", 0) or usage.get("prompt_tokens", 0) or 0)
            if turn_tokens:
                total_tokens += turn_tokens
                self.budget.add_tokens(turn_tokens)
            else:
                # Estimate
                est = len((llm_resp.content or "")) // 4
                total_tokens += est
                self.budget.add_tokens(est)

            # Append assistant message to history
            assistant_content = llm_resp.content or ""
            # Antes de declarar \"vacío\", intentar extraer tool_calls de texto
            # (modelos locales ponen <tool_call>JSON</tool_call> en reasoning_content)
            _pre_tcs: list = []
            if not llm_resp.has_tool_calls and not assistant_content.strip():
                _pre_tcs = self._extract_text_tool_calls(getattr(llm_resp, "reasoning_content", "") or "")
                # también probar el fallback <tool_call> que ya extrae LlamaCppProvider arriba,
                # pero por si el provider no lo pobló, usar extracción textual
                if not _pre_tcs:
                    _src = getattr(llm_resp, "reasoning_content", "") or ""
                    if "<tool_call" in _src:
                        import re as _re_pre, json as _json_pre
                        for _m in _re_pre.finditer(r"<tool_call[^>]*>\s*(\{.*?\})\s*</tool_call>", _src, flags=_re_pre.DOTALL):
                            try:
                                _obj = _json_pre.loads(_m.group(1))
                                _name = str(_obj.get("name", _obj.get("tool", ""))).strip()
                                _args = _obj.get("arguments", _obj.get("args", _obj.get("parameters", {})))
                                if isinstance(_args, str):
                                    try: _args = _json_pre.loads(_args) if _args else {}
                                    except Exception: _args = {}
                                if _name and isinstance(_args, dict):
                                    from .llm_provider import ToolCall as _TCPre
                                    _pre_tcs.append(_TCPre(id=f"pre-{len(_pre_tcs)}", name=_name, arguments=_args))
                            except Exception:
                                continue
                if _pre_tcs:
                    # hay tool calls reales — no tratar como vacío, dejar que el flujo normal los despache
                    pass
                else:
                    _reason = (getattr(llm_resp, "reasoning_content", "") or "").strip()
                    _empty_retries = getattr(self, "_empty_retries", 0)
                    if _reason:
                        steps.append(LoopStep(iteration=iteration, phase="decide", content="razonamiento sin respuesta final: se continúa"))
                        messages.append(LLMMessage(role="assistant", content=f"[razonamiento interno: {_reason[:2000]}]"))
                        messages.append(LLMMessage(role="user", content="Continúa: usa tus herramientas si la tarea lo requiere, o da tu respuesta final."))
                        continue
                    elif _empty_retries < 2:
                        self._empty_retries = _empty_retries + 1  # type: ignore[attr-defined]
                        steps.append(LoopStep(iteration=iteration, phase="decide", content=f"respuesta vacía ({self._empty_retries}/2): reintentando"))
                        messages.append(LLMMessage(role="user", content="Tu respuesta anterior llegó vacía. Responde de nuevo, de forma concisa."))
                        continue
            messages.append(LLMMessage(role="assistant", content=assistant_content, tool_calls=llm_resp.tool_calls if llm_resp.has_tool_calls else ()))

            # Trajectory
            if llm_resp.has_tool_calls:
                for tc in llm_resp.tool_calls:
                    trajectory.append_assistant(assistant_content, iteration=iteration, tool_name=tc.name, tool_call_id=tc.id, tool_args=tc.arguments)
            else:
                trajectory.append_assistant(assistant_content, iteration=iteration)

            # Session log
            if self.session_log is not None:
                try:
                    from .session_log import Message
                    self.session_log.append(session_id, Message(role="assistant", content=assistant_content, model=getattr(llm_resp, "model", ""), tokens=turn_tokens))
                except Exception:
                    pass

            # ---- DECIDE: tool_calls vs final ----
            _tcs = list(llm_resp.tool_calls) if llm_resp.has_tool_calls else []
            if not _tcs:
                # Respaldo: modelos pequeños escupen JSON en texto (content o
                # reasoning_content) en vez de tool_calls.
                # {"tool":"echo","arguments":{...}} o {"tool":"echo","text":"..."}
                _tcs = self._extract_text_tool_calls(assistant_content)
                if not _tcs:
                    _tcs = self._extract_text_tool_calls(getattr(llm_resp, "reasoning_content", "") or "")
                if _tcs:
                    steps.append(LoopStep(iteration=iteration, phase="decide", content=f"tool_calls extraídos de texto: {len(_tcs)}"))
            if not _tcs:
                # Final answer
                steps.append(LoopStep(iteration=iteration, phase="decide", content=f"respuesta final ({len(assistant_content)} caracteres)"))
                final_output = assistant_content
                success = True
                break
            # ---- ACT: despachar cada llamada de herramienta ----
            # Usa _tcs (reales o extraídos del texto) en vez de llm_resp.tool_calls
            for tc in _tcs:
                tool_name = tc.name
                tool_args = tc.arguments if isinstance(tc.arguments, dict) else {}
                # If arguments is a JSON string (from llama.cpp), parse it
                if isinstance(tc.arguments, str):
                    try:
                        tool_args = json.loads(tc.arguments) if tc.arguments else {}
                    except Exception:
                        tool_args = {}
                steps.append(LoopStep(iteration=iteration, phase="decide", content=f"decide: {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:300]})", tool_used=tool_name))

                # Act
                tool_result_str = ""
                tool_success = False
                try:
                    if self.tools is None:
                        raise KeyError(f"sin registro de herramientas; no se puede despachar {tool_name}")
                    # Dispatch with scope+hooks
                    raw_result = await self.tools.dispatch(tool_name, tool_args, scope=self.scope, hooks=self.hooks)
                    # Normalize result to string
                    if isinstance(raw_result, str):
                        tool_result_str = raw_result
                    elif isinstance(raw_result, dict):
                        tool_result_str = json.dumps(raw_result, ensure_ascii=False, indent=2)
                    elif raw_result is None:
                        tool_result_str = ""
                    else:
                        tool_result_str = str(raw_result)
                    tool_success = True
                    self.budget.add_tool_calls(1)
                    # Anti doom-loop (aden-hive): 3 llamadas idénticas consecutivas → cortar
                    try:
                        _fp = (tool_name, json.dumps(tool_args if isinstance(tool_args, dict) else {}, sort_keys=True, default=str))
                        _win = getattr(self, "_tool_fp_window", [])
                        _win.append(_fp)
                        self._tool_fp_window = _win[-3:]  # type: ignore[attr-defined]
                        if len(self._tool_fp_window) == 3 and all(x == _win[0] for x in self._tool_fp_window):
                            final_output = f"[doom-loop: {tool_name} repetido 3 veces con mismos args — se detiene. Resultado ya entregado.]"
                            try:
                                messages.append(LLMMessage(role="tool", content=tool_result_str, tool_call_id=getattr(tc, 'id', '')))
                            except Exception:
                                pass
                            success = True
                            iteration = max_iters  # force exit
                            break
                    except Exception:
                        pass
                except Exception as e:
                    err_str = str(e)
                    tool_result_str = f"Error: {err_str}"
                    tool_success = False
                    # Classify error
                    action = self._classify_error(tool_name, err_str)
                    steps.append(LoopStep(iteration=iteration, phase="act", content=f"herramienta {tool_name} falló: {err_str} -> {action.value}", tool_used=tool_name, result=ToolResult(success=False, output="", error=err_str)))
                    if action == ErrorAction.ABORT:
                        final_output = f"[abortado: {tool_name} -> {err_str}]"
                        success = False
                        # Break outer loop
                        iteration = max_iters  # force exit
                        break
                    elif action == ErrorAction.RETRY:
                        cnt = self._retry_counts.get(tool_name, 0)
                        if cnt < error_retry_limit:
                            self._retry_counts[tool_name] = cnt + 1
                            # Se reintentará devolviendo el error al LLM como resultado de herramienta and continuing
                        else:
                            tool_result_str += f" [presupuesto reintentos agotado {cnt}]"
                    elif action == ErrorAction.REPLAN:
                        cnt = self._replan_counts.get(tool_name, 0)
                        if cnt < error_replan_limit:
                            self._replan_counts[tool_name] = cnt + 1
                        else:
                            tool_result_str += f" [presupuesto replanificación agotado]"
                    elif action == ErrorAction.SKIP:
                        tool_result_str = f"[omitido {tool_name}: {err_str}]"
                        tool_success = True  # treat as non-fatal

                # Truncate very large results for context
                if len(tool_result_str) > 8000:
                    tool_result_str = tool_result_str[:8000] + f"\n[...truncado {len(tool_result_str)-8000} caracteres]"

                # Observar: agregar resultado de herramienta to messages + session + trajectory
                steps.append(LoopStep(iteration=iteration, phase="act", content=f"act: {tool_name}", tool_used=tool_name, result=ToolResult(success=tool_success, output=tool_result_str[:2000])))
                trajectory.append_tool_result(tool_name, tool_result_str, success=tool_success, iteration=iteration, tool_call_id=tc.id)
                messages.append(LLMMessage(role="tool", content=tool_result_str, tool_call_id=tc.id))
                if self.session_log is not None:
                    try:
                        from .session_log import Message
                        self.session_log.append(session_id, Message(role="tool", content=tool_result_str, tool_name=tool_name, tool_call_id=tc.id))
                    except Exception:
                        pass
                if self.event_store is not None:
                    try:
                        self.event_store.append_event("tool_result" if tool_success else "error", {"tool": tool_name, "result": tool_result_str[:2000], "success": tool_success}, session_id=session_id)
                    except Exception:
                        pass

            # ---- OBSERVE: budget check + compaction ----
            if self.budget.exhausted:
                steps.append(LoopStep(iteration=iteration, phase="observe", content=f"presupuesto agotado tras iter {iteration}: {self.budget.reason}"))
                final_output = final_output or "[presupuesto agotado antes de respuesta final]"
                break

            # Maybe compact if approaching limit
            if self.compaction is not None and len(messages) > 10:
                try:
                    dict_msgs = [{"role": m.role, "content": m.content or ""} for m in messages]
                    max_tokens = 32000
                    if self.compaction.maybe_compact(dict_msgs, max_tokens):
                        # Solo compactar system+historial, mantener resultados de herramientas recientes
                        dict_msgs, _ = await self.compaction.compact(dict_msgs, int(max_tokens * 0.75))
                        messages = [LLMMessage(role=d["role"], content=d.get("content")) for d in dict_msgs]
                        steps.append(LoopStep(iteration=iteration, phase="observe", content=f"contexto compactado ({len(dict_msgs)} mensajes)"))
                except Exception as e:
                    logger.debug(f"observe compaction falló: {e}")

        # Finalize
        duration_ms = (time.monotonic() - t0) * 1000
        if not final_output and trajectory.steps:
            # Fallback: last assistant content
            last = trajectory.last_assistant
            if last:
                final_output = last.content
                success = bool(final_output and "error" not in final_output.lower()[:100])

        if self.event_store is not None:
            try:
                self.event_store.append_event("assistant_message" if success else "error", {"content": final_output[:2000], "success": success, "iterations": iteration}, session_id=session_id)
            except Exception:
                pass
        if self.hooks is not None:
            try:
                await self.hooks.emit("session_end", {"session_id": session_id, "success": success, "iterations": iteration, "total_tokens": total_tokens})
            except Exception:
                pass
        # Session summary update
        if self.session_log is not None and final_output:
            try:
                # Don't overwrite existing summary if already set via compact
                sess = self.session_log.get_session(session_id)
                if sess is not None and not sess.summary:
                    self.session_log.update_session(session_id, summary=final_output[:500])
            except Exception:
                pass

        return LoopResult(
            task=task,
            success=success,
            final_output=final_output,
            steps=steps,
            iterations=iteration,
            total_tokens=total_tokens,
            duration_ms=duration_ms,
            trajectory=trajectory,
        )

    def _classify_error(self, tool: str, err: str) -> ErrorAction:
        try:
            return classify_error(tool, err)
        except Exception:
            return ErrorAction.REPLAN

    def set_error_policy(self, retry: int = 3, replan: int = 2) -> None:
        self._error_policy = {"retry": retry, "replan": replan}

    def reset_budget(self, max_iterations: int | None = None, max_tokens: int | None = None) -> None:
        if max_iterations is not None:
            self.budget.max_iterations = max_iterations
        if max_tokens is not None:
            self.budget.max_tokens = max_tokens
        self.budget.reset()
        self._retry_counts.clear()
        self._replan_counts.clear()
