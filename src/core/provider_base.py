"""Provider Base — interfaz unificada (frozen dataclasses + ABC).

Patron: Pincer (frozen+slots) + SuperNEXUS v2 provider_base.py + Hermes model_metadata.
Todos los providers (Ollama, llama.cpp, Cloud) implementan BaseProvider.

Ver BLUEPRINT.md §10.4
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


class MessageRole:
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    role: str = "tool"
    tool_call_id: str = ""
    name: str = ""
    content: str = ""


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: str = "user"
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""
    # Adjuntos visuales (DECK): data URIs "data:image/png;base64,..." en el
    # mensaje de usuario. Ollama los recibe como base64 crudo; OpenAI-compat
    # como content parts image_url. None = mensaje sin imagen.
    images: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    reasoning_content: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class BaseProvider(abc.ABC):
    """Contrato que todo provider debe cumplir."""

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        **kw: Any,
    ) -> LLMResponse:
        ...

    @abc.abstractmethod
    async def health(self) -> bool:
        """Probe rapido (GET /health o /api/tags)."""
        ...

    @abc.abstractmethod
    async def list_models(self) -> list[str]:
        ...

    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        **kw: Any,
    ):
        """Async generator de deltas: {"type":"text","text":...} y {"type":"final","response":LLMResponse}.

        Default (sin streaming real): delega a chat() y emite el contenido completo de una vez.
        Los providers con SSE real lo sobreescriben.
        """
        resp = await self.chat(messages, tools=tools, model=model, **kw)
        if getattr(resp, "content", None):
            yield {"type": "text", "text": resp.content}
        yield {"type": "final", "response": resp}

