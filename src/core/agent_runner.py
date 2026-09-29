"""
AgentRunner — Bucle de tool-calling reutilizable e independiente del modelo.

Patrón: nanobot AgentRunner + pircer LLMProvider + OpenAI-compatible spec.
Capa intermedia entre el Director y el LLM: el Director ya no llama al LLM directo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

# Loop guard tunables (openfang pattern). Hash (name+canonical-args) of each
# tool call and keep the last LOOP_GUARD_WINDOW. If the same hash appears
# >= LOOP_GUARD_THRESHOLD times inside that window, we're in a doom loop:
# break out, return a diagnostic stop_reason instead of bleeding tokens.
LOOP_GUARD_WINDOW = 6
LOOP_GUARD_THRESHOLD = 3

EMPTY_FINAL_RESPONSE_MESSAGE = (
    "El modelo no produjo una respuesta final. "
    "Esto puede deberse a un timeout, un filtro de seguridad o un error del proveedor. "
    "Reintenta la solicitud o cambia de gema."
)


def is_blank_text(content: str | None) -> bool:
    """True when *content* is missing or only whitespace. Patron de nanobot."""
    return content is None or not content.strip()


def _tool_call_hash(tc: "ToolCall") -> str:
    """Stable hash of (tool name, canonical args). Order-insensitive on dict keys."""
    try:
        payload = json.dumps(
            {"n": tc.name, "a": tc.arguments},
            sort_keys=True, default=str, ensure_ascii=False,
        )
    except Exception:
        payload = f"{tc.name}|{tc.arguments!r}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

from src.core.provider_base import (
    LLMProvider,
    LLMResponse,
    LLMMessage,
    MessageRole,
    ToolCall,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentRunSpec:
    messages: list[LLMMessage]
    tools_definitions: list[dict[str, Any]]
    model: str = ""
    max_iterations: int = 20
    max_tool_result_chars: int = 4000
    temperature: float | None = None
    max_tokens: int | None = None
    error_message: str = "Lo siento, ocurrio un error al procesar tu solicitud."
    fallback_models: list[str] = field(default_factory=list)
    context_window_tokens: int = 8192
    on_stream: Callable[[str], None] | None = None
    on_tool_start: Callable[[str, dict], None] | None = None
    on_tool_end: Callable[[str, Any], None] | None = None
    checkpoint_callback: Callable[[dict], None] | None = None


@dataclass(slots=True)
class AgentRunResult:
    content: str | None = None
    messages: list[LLMMessage] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    iterations: int = 0
    error: str | None = None


class AgentRunner:
    """Runner agnóstico: funciona con cualquier LLMProvider.

    Uso:
        runner = AgentRunner(provider)
        result = await runner.run(AgentRunSpec(
            messages=[LLMMessage(role="user", content="Hola")],
            tools_definitions=[...],
            model="qwen2.5:7b",
        ))

    tool_executor: callable opcional para ejecutar tools externamente.
        Firma: async (name: str, args: dict) -> Any
        Si no se provee, devuelve stub "tool not available".
    """

    def __init__(self, provider: LLMProvider, tool_executor: Callable[[str, dict], Awaitable[Any]] | None = None):
        self.provider = provider
        self._tool_executor = tool_executor

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        messages = list(spec.messages)
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        stop_reason = "completed"
        tool_hash_window: list[str] = []  # loop guard sliding window

        for iteration in range(spec.max_iterations):
            if spec.on_tool_start:
                pass

            response = await self._request_model(spec, messages)

            self._accumulate_usage(usage, response.usage)

            # Fallback: detectar JSON tool_call en texto plano (modelos locales)
            if not response.should_execute_tools and response.content:
                parsed = self._detect_text_tool_call(response.content)
                if parsed:
                    tc = ToolCall(id=parsed["name"], name=parsed["name"], arguments=parsed["arguments"])
                    response = LLMResponse(
                        content=response.content,
                        tool_calls=(tc,),
                        finish_reason=response.finish_reason,
                        usage=response.usage,
                        model=response.model,
                    )

            if response.should_execute_tools:
                # Loop guard: hash each call, slide window, abort on repetition.
                for tc in response.tool_calls:
                    h = _tool_call_hash(tc)
                    tool_hash_window.append(h)
                    if len(tool_hash_window) > LOOP_GUARD_WINDOW:
                        tool_hash_window = tool_hash_window[-LOOP_GUARD_WINDOW:]
                    if tool_hash_window.count(h) >= LOOP_GUARD_THRESHOLD:
                        logger.warning(
                            "tool_loop_detected name=%s hash=%s window=%s",
                            tc.name, h, tool_hash_window,
                        )
                        try:
                            from src.observability.event_stream import emit, EventType
                            emit(EventType.TOOL_LOOP_DETECTED,
                                 data={"tool": tc.name, "hash": h,
                                       "window_size": len(tool_hash_window),
                                       "threshold": LOOP_GUARD_THRESHOLD},
                                 source="agent_runner")
                        except Exception:
                            pass
                        stop_reason = "tool_loop_detected"
                        final_content = (
                            f"Tool '{tc.name}' fue invocado {LOOP_GUARD_THRESHOLD} "
                            f"veces con los mismos argumentos en las ultimas "
                            f"{LOOP_GUARD_WINDOW} llamadas. Loop detectado y "
                            f"abortado para evitar consumo de recursos."
                        )
                        error = final_content
                        break
                if stop_reason == "tool_loop_detected":
                    break

                for tc in response.tool_calls:
                    tools_used.append(tc.name)

                assistant_msg = LLMMessage(
                    role=MessageRole.assistant,
                    content=response.content,
                    tool_calls=tuple(response.tool_calls),
                )
                messages.append(assistant_msg)

                if spec.on_tool_start:
                    for tc in response.tool_calls:
                        spec.on_tool_start(tc.name, tc.arguments)

                all_results = await self._execute_tools(spec, response.tool_calls)

                for tc, result in zip(response.tool_calls, all_results):
                    tool_msg = LLMMessage(
                        role=MessageRole.tool,
                        content=self._normalize_result(spec, result),
                        tool_call_id=tc.id,
                    )
                    messages.append(tool_msg)
                    if spec.on_tool_end:
                        spec.on_tool_end(tc.name, result)

                if spec.checkpoint_callback:
                    spec.checkpoint_callback({
                        "iteration": iteration,
                        "phase": "tools_completed",
                        "assistant_msg": assistant_msg,
                        "tool_results": [r for r in all_results],
                    })

                continue

            if response.has_tool_calls:
                logger.warning("Tool calls ignored under finish_reason=%s", response.finish_reason)

            if response.finish_reason == "error":
                error = response.content or spec.error_message
                final_content = error
                stop_reason = "error"
                break

            final_content = response.content
            if is_blank_text(final_content):
                stop_reason = "empty_final_response"
                error = EMPTY_FINAL_RESPONSE_MESSAGE
            messages.append(LLMMessage(
                role=MessageRole.assistant,
                content=final_content or "",
            ))
            if spec.on_stream and final_content:
                spec.on_stream(final_content)
            break
        else:
            stop_reason = "max_iterations"
            final_content = f"Maximo de {spec.max_iterations} iteraciones alcanzado."
            error = final_content

        usage["iterations"] = iteration + 1
        return AgentRunResult(
            content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            iterations=iteration + 1,
            error=error,
        )

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[LLMMessage],
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": spec.tools_definitions if spec.tools_definitions else None,
        }
        if spec.model:
            kwargs["model"] = spec.model
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens

        if spec.on_stream and self.provider.supports_streaming:
            collected: list[str] = []
            # Scrub think blocks from streaming output
            try:
                from src.core.think_scrubber import StreamingThinkScrubber
                _scrubber = StreamingThinkScrubber()
            except ImportError:
                _scrubber = None
            def on_chunk(chunk: str) -> None:
                if _scrubber:
                    visible = _scrubber.feed(chunk)
                    if visible:
                        collected.append(visible)
                        spec.on_stream(visible)
                else:
                    collected.append(chunk)
                    spec.on_stream(chunk)
            kwargs["on_content"] = on_chunk
            resp = await self.provider.chat_stream(**kwargs)
            # Flush any remaining scrubber state
            if _scrubber:
                tail = _scrubber.flush()
                if tail:
                    collected.append(tail)
                    spec.on_stream(tail)
            return resp

        return await self.provider.chat_with_retry(**kwargs)

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCall],
    ) -> list[Any]:
        results: list[Any] = []
        for tc in tool_calls:
            result = await self._run_tool(spec, tc)
            results.append(result)
        return results

    async def _run_tool(self, spec: AgentRunSpec, tc: ToolCall) -> Any:
        if self._tool_executor:
            try:
                return await self._tool_executor(tc.name, tc.arguments)
            except Exception as e:
                logger.error("Tool %s error: %s", tc.name, e)
                return f"Error: {type(e).__name__}: {e}"
        return f"Tool '{tc.name}' not available (no tool_executor configured)"

    @staticmethod
    def _detect_text_tool_call(text: str) -> dict | None:
        """Detecta tool_calls en texto plano tipo JSON (fallback modelos locales).

        Detecta dos formatos:
        1. {"name": "tool_name", "arguments": {...}}  (formato estándar)
        2. {"tool_name": {"arg1": "val1"}}  (formato alternativo del LLM)
        """
        # Quitar wrappers markdown
        text = re.sub(r'```(?:json)?\s*|\s*```', '', text)

        # Formato 1: {"name": "...", "arguments": {...}}  OR  {"tool": "...", "params": {...}}
        m = re.search(r'\{[\s]*"?(?:name|tool)"?\s*:\s*"[^"]+"\s*,\s*"(?:arguments|params)"\s*:', text)
        if m:
            start = m.start()
            depth, end = 0, start
            for i in range(start, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > start:
                try:
                    obj = json.loads(text[start:end])
                    if isinstance(obj, dict):
                        name = obj.get("name") or obj.get("tool")
                        args = obj.get("arguments") or obj.get("params") or {}
                        if name:
                            return {"name": name, "arguments": args}
                except json.JSONDecodeError:
                    pass

        # Formato 2: {"tool_name": {"arg1": "val1"}}  (sin "name"/"arguments" keys)
        m = re.search(r'\{"([a-z_]+)"\s*:\s*\{', text)
        if m:
            start = m.start()
            depth, end = 0, start
            for i in range(start, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > start:
                try:
                    obj = json.loads(text[start:end])
                    if isinstance(obj, dict):
                        for key, val in obj.items():
                            if isinstance(val, dict):
                                return {"name": key, "arguments": val}
                except json.JSONDecodeError:
                    pass

        # Formato 3: tool_name(arg1="val1", arg2="val2") — Python-style function call
        m = re.search(r'([a-z_]+)\s*\(([^)]*)\)', text, re.IGNORECASE)
        if m:
            tool_name = m.group(1)
            known_tools = {"research_scholar", "web_search", "web_fetch", "read_file", "write_file",
                          "execute_command", "list_dir", "search_files", "find_files", "browser"}
            if tool_name in known_tools:
                args_str = m.group(2)
                args = {}
                # Parse key="value" or key=value patterns
                for arg_match in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\w+))', args_str):
                    key = arg_match.group(1)
                    val = arg_match.group(2) or arg_match.group(3) or arg_match.group(4)
                    # Convert booleans
                    if val.lower() == 'true': val = True
                    elif val.lower() == 'false': val = False
                    args[key] = val
                return {"name": tool_name, "arguments": args}

        # Formato 4: **Tool Call:** `tool_name`  (solo mención del tool sin JSON)
        m = re.search(r'`([a-z_]+)`', text, re.IGNORECASE)
        if m:
            tool_name = m.group(1)
            known_tools = {"research_scholar", "web_search", "web_fetch", "read_file", "write_file",
                          "execute_command", "list_dir", "search_files", "find_files", "browser"}
            if tool_name in known_tools:
                # Try to extract arguments from the text
                args = {}
                q_match = re.search(r'(?:query|q|search)\s*[:=]\s*["\']([^"\']+)["\']', text, re.IGNORECASE)
                if q_match:
                    args["query"] = q_match.group(1)
                return {"name": tool_name, "arguments": args}

        return None

    @staticmethod
    def _normalize_result(spec: AgentRunSpec, result: Any) -> str:
        if result is None:
            return "(empty)"
        text = str(result)
        if len(text) > spec.max_tool_result_chars:
            return text[:spec.max_tool_result_chars] + "\n...(truncated)"
        return text

    @staticmethod
    def _accumulate_usage(target: dict, addition: dict) -> None:
        for k, v in addition.items():
            if isinstance(v, (int, float)):
                target[k] = target.get(k, 0) + int(v)
