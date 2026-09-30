import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import httpx

from .provider_capabilities import ProviderCapabilities, ToolsPayloadKind, context_window_for_model

logger = logging.getLogger(__name__)


class AuthStyle(Enum):
    NONE = "none"
    BEARER = "bearer"
    X_API_KEY = "x_api_key"
    ANTHROPIC = "anthropic"
    CUSTOM = "custom"

    @classmethod
    def from_config(cls, style: str) -> "AuthStyle":
        mapping = {
            "none": cls.NONE,
            "bearer": cls.BEARER,
            "x-api-key": cls.X_API_KEY,
            "anthropic": cls.ANTHROPIC,
            "custom": cls.CUSTOM,
        }
        return mapping.get(style.lower(), cls.BEARER)


@dataclass
class ChatMessage:
    id: Optional[str] = None
    role: str = "user"
    content: str = ""
    extra_metadata: Optional[dict] = None

    @classmethod
    def system(cls, content: str) -> "ChatMessage":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> "ChatMessage":
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> "ChatMessage":
        return cls(role="assistant", content=content)

    @classmethod
    def tool(cls, content: str) -> "ChatMessage":
        return cls(role="tool", content=content)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str
    extra_content: Optional[dict] = None


@dataclass
class UsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0
    context_window: int = 0
    cached_input_tokens: int = 0
    charged_amount_usd: float = 0.0


@dataclass
class ChatResponse:
    text: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Optional[UsageInfo] = None
    reasoning_content: Optional[str] = None

    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    def text_or_empty(self) -> str:
        return self.text or ""


class ProviderDelta:
    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_ARGS = "tool_call_args"


@dataclass
class DeltaEvent:
    kind: str
    delta: str = ""
    call_id: str = ""
    tool_name: str = ""


class BaseProvider:
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    async def chat(self, messages: list[ChatMessage], model: str, temperature: float = 0.7,
                   tools: Optional[list[dict]] = None, stream_callback=None,
                   system_prompt: Optional[str] = None) -> ChatResponse:
        raise NotImplementedError

    async def simple_chat(self, message: str, model: str, temperature: float = 0.7) -> str:
        raise NotImplementedError

    def supports_native_tools(self) -> bool:
        return self.capabilities().native_tool_calling

    def supports_vision(self) -> bool:
        return self.capabilities().vision

    def context_window(self, model: str) -> int:
        return context_window_for_model(model)


class UnifiedProvider(BaseProvider):
    def __init__(self, name: str, base_url: str, api_key: Optional[str] = None,
                 auth_style: AuthStyle = AuthStyle.BEARER,
                 supports_tools: bool = True, supports_vision: bool = True,
                 merge_system: bool = False, api_path: str = "/v1/chat/completions",
                 user_agent: Optional[str] = None, extra_headers: Optional[dict] = None,
                 temperature_override: Optional[float] = None,
                 temperature_unsupported_models: Optional[list] = None,
                 enforce_num_ctx: Optional[int] = None,
                 local_provider: bool = False):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.auth_style = auth_style
        self._supports_tools = supports_tools
        self._supports_vision = supports_vision
        self.merge_system = merge_system
        self.api_path = api_path
        self.user_agent = user_agent or "supernexus-ai/1.0"
        self._extra_headers = extra_headers or {}
        self.temperature_override = temperature_override
        self.temperature_unsupported_models = temperature_unsupported_models or []
        self.enforce_num_ctx = enforce_num_ctx
        self.local_provider = local_provider
        self._http = httpx.AsyncClient(timeout=120.0, follow_redirects=True)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_tool_calling=self._supports_tools,
            vision=self._supports_vision,
        )

    def _effective_temperature(self, model: str, caller_temp: float) -> Optional[float]:
        temp = self.temperature_override if self.temperature_override is not None else caller_temp
        for pattern in self.temperature_unsupported_models:
            if pattern == "*" or pattern in model:
                return None
        return temp

    def _apply_auth(self, headers: dict) -> dict:
        # FIX 2026-08-13: nunca enviar headers de auth vacíos
        # (HTTP rechaza 'Authorization: Bearer ' como valor ilegal — rompía
        # el fallback a Ollama que no necesita key). Solo añadir si hay key.
        if self.auth_style == AuthStyle.BEARER:
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
        elif self.auth_style == AuthStyle.X_API_KEY:
            if self._api_key:
                headers["x-api-key"] = self._api_key
        elif self.auth_style == AuthStyle.ANTHROPIC:
            if self._api_key:
                headers["x-api-key"] = self._api_key
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def _chat_completions_url(self) -> str:
        return f"{self.base_url}{self.api_path}"

    def _build_messages(self, messages: list[ChatMessage], system_prompt: Optional[str] = None) -> list[dict]:
        result = []
        sys_msg = None
        for m in messages:
            if m.role == "system":
                sys_msg = m.content
            else:
                result.append({"role": m.role, "content": m.content})
        if system_prompt or sys_msg:
            final_system = system_prompt or sys_msg or ""
            if self.merge_system:
                if result:
                    result[0]["content"] = f"{final_system}\n\n{result[0]['content']}"
            else:
                result.insert(0, {"role": "system", "content": final_system})
        if not result:
            result.append({"role": "user", "content": ""})
        return result

    def _build_tools(self, tools: Optional[list[dict]]) -> Optional[list[dict]]:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {}),
                },
            }
            for t in tools
        ]

    def _parse_response(self, data: dict) -> ChatResponse:
        choice = data.get("choices", [{}])[0] if data.get("choices") else {}
        delta = choice.get("delta", {})
        message = choice.get("message", delta)
        text = message.get("content", "")
        reasoning = message.get("reasoning_content") or choice.get("reasoning_content") or None
        raw_calls = message.get("tool_calls", [])
        tool_calls = []
        for tc in raw_calls:
            tid = tc.get("id", "")
            fn = tc.get("function", {})
            args_raw = fn.get("arguments", "{}")
            if isinstance(args_raw, dict):
                args_raw = json.dumps(args_raw)
            tool_calls.append(ToolCall(id=tid, name=fn.get("name", ""), arguments=args_raw))
        usage_raw = data.get("usage", {})
        usage = None
        if usage_raw:
            usage = UsageInfo(
                input_tokens=usage_raw.get("prompt_tokens", 0),
                output_tokens=usage_raw.get("completion_tokens", 0),
                context_window=self.context_window(""),
                cached_input_tokens=usage_raw.get("prompt_tokens_details", {}).get("cached_tokens", 0) if isinstance(usage_raw.get("prompt_tokens_details"), dict) else 0,
                charged_amount_usd=data.get("openhuman", {}).get("billing", {}).get("charged_amount_usd", 0.0) if isinstance(data.get("openhuman"), dict) and isinstance(data.get("openhuman", {}).get("billing"), dict) else 0.0,
            )
        return ChatResponse(text=text or None, tool_calls=tool_calls, usage=usage, reasoning_content=reasoning)

    async def _post(self, url: str, json_data: dict) -> httpx.Response:
        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
        }
        self._apply_auth(headers)
        headers.update(self._extra_headers)
        return await self._http.post(url, json=json_data, headers=headers)

    async def chat(self, messages: list[ChatMessage], model: str, temperature: float = 0.7,
                   tools: Optional[list[dict]] = None, stream_callback=None,
                   system_prompt: Optional[str] = None) -> ChatResponse:
        body = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "stream": stream_callback is not None,
        }
        temp = self._effective_temperature(model, temperature)
        if temp is not None:
            body["temperature"] = temp
        built_tools = self._build_tools(tools)
        if built_tools:
            body["tools"] = built_tools
            body["tool_choice"] = "auto"
        if self.enforce_num_ctx:
            body.setdefault("options", {}).setdefault("num_ctx", self.enforce_num_ctx)
        url = self._chat_completions_url()
        try:
            resp = await self._post(url, body)
            resp.raise_for_status()
            data = resp.json()
            return self._parse_response(data)
        except httpx.HTTPStatusError as e:
            body_text = await e.response.aread()
            logger.error("[%s] HTTP %d: %s", self.name, e.response.status_code, body_text.decode(errors="replace")[:500])
            raise

    async def simple_chat(self, message: str, model: str, temperature: float = 0.7) -> str:
        resp = await self.chat([ChatMessage.user(message)], model, temperature)
        return resp.text_or_empty()

    async def close(self):
        await self._http.aclose()


class AnthropicProvider(BaseProvider):
    def __init__(self, name: str, api_key: Optional[str] = None,
                 base_url: str = "https://api.anthropic.com"):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=120.0)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_tool_calling=True, vision=True)

    async def chat(self, messages: list[ChatMessage], model: str, temperature: float = 0.7,
                   tools: Optional[list[dict]] = None, stream_callback=None,
                   system_prompt: Optional[str] = None) -> ChatResponse:
        system_text = system_prompt or ""
        api_messages = []
        for m in messages:
            if m.role == "system":
                system_text = system_text or m.content
            elif m.role == "tool":
                api_messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.id or "", "content": m.content}]})
            else:
                api_messages.append({"role": m.role, "content": m.content})
        if not api_messages:
            api_messages.append({"role": "user", "content": ""})
        body: dict = {
            "model": model,
            "max_tokens": 4096,
            "messages": api_messages,
            "temperature": temperature,
        }
        if system_text:
            body["system"] = system_text
        if tools:
            body["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": {"type": "object", "properties": t.get("parameters", {}).get("properties", {}),
                                  "required": t.get("required", [])}}
                for t in tools
            ]
        headers = {
            "x-api-key": self._api_key or "",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/v1/messages"
        resp = await self._http.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        text_parts = []
        tool_calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                args = block.get("input", {})
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=json.dumps(args) if isinstance(args, dict) else str(args),
                ))
        usage = None
        if data.get("usage"):
            u = data["usage"]
            usage = UsageInfo(
                input_tokens=u.get("input_tokens", 0),
                output_tokens=u.get("output_tokens", 0),
            )
        return ChatResponse(
            text="\n".join(text_parts) or None,
            tool_calls=tool_calls,
            usage=usage,
        )

    async def simple_chat(self, message: str, model: str, temperature: float = 0.7) -> str:
        resp = await self.chat([ChatMessage.user(message)], model, temperature)
        return resp.text_or_empty()


class ProviderFactory:
    @staticmethod
    def from_provider_string(ps: str, api_keys: Optional[dict] = None) -> tuple[Optional[BaseProvider], str]:
        api_keys = api_keys or {}
        if ps.startswith("ollama:"):
            from src.core.environment import ollama_base_url
            url = ollama_base_url()
            model = ps.split(":", 1)[1].split("@")[0].strip()
            return UnifiedProvider(
                name="ollama",
                base_url=url,
                auth_style=AuthStyle.NONE,
                supports_tools=False,
                supports_vision=True,
                local_provider=True,
            ), model
        if ps.startswith("llama:"):
            # llama.cpp opt-in via llama-server OpenAI-compatible (si el usuario lo configura)
            # ps = llama:<ruta-o-alias> ; base_url desde env LLAMA_CPP_URL ; sin mapeos de path
            import os
            base_url = os.environ.get("LLAMA_CPP_URL", "http://localhost:8080")
            base_url = base_url.rstrip("/").removesuffix("/v1")
            model = ps.split(":", 1)[1].split("@")[0].strip()
            return UnifiedProvider(
                name="llama",
                base_url=base_url,
                auth_style=AuthStyle.NONE,
                supports_tools=True,
                supports_vision=True,
                local_provider=True,
            ), model
        base = ps.split("@")[0]
        return None, base

    @staticmethod
    def create_openai_compatible(name: str, base_url: str, api_key: Optional[str] = None,
                                  auth_style: AuthStyle = AuthStyle.BEARER,
                                  supports_tools: bool = True, supports_vision: bool = True,
                                  **kwargs) -> UnifiedProvider:
        return UnifiedProvider(
            name=name, base_url=base_url, api_key=api_key,
            auth_style=auth_style, supports_tools=supports_tools,
            supports_vision=supports_vision, **kwargs,
        )
