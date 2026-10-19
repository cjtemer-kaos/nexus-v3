"""Providers mock compartidos (traducidos de run_tests.py secciones 3 y 4)."""
from __future__ import annotations

import time
from pathlib import Path

from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse, ToolCall


def remove_path(path, tries: int = 30, delay: float = 0.04) -> bool:
    """Borra `path` tolerando locks transitorios del SO (en Windows: WinError 32).

    Un archivo recien creado puede quedar bloqueado unos milisegundos por el
    escaner/indexador del sistema, asi que el `unlink` inmediato falla aunque
    nadie lo tenga abierto de forma sostenida. Se reintenta con backoff.
    Devuelve True si el archivo ya no esta; False si siguio bloqueado.
    """
    p = Path(path)
    for i in range(tries):
        try:
            p.unlink(missing_ok=True)
            return True
        except OSError:
            time.sleep(delay * (1 + i // 5))
    return not p.exists()


def write_fresh(path, text: str, encoding: str = "utf-8", tries: int = 30, delay: float = 0.04) -> None:
    """Escribe `path` desde cero, tolerando que un residuo previo este bloqueado."""
    p = Path(path)
    last: OSError | None = None
    for i in range(tries):
        try:
            remove_path(p)
            p.write_text(text, encoding=encoding)
            return
        except OSError as e:
            last = e
            time.sleep(delay * (1 + i // 5))
    if last is not None:
        raise last


class MockProvider(BaseProvider):
    def __init__(self, name, healthy=True, models=None, fail=False):
        self.name = name
        self._healthy = healthy
        self._models = models or [f"{name}-model"]
        self.fail = fail

    async def chat(self, messages, tools=None, model=None, **kw):
        if self.fail:
            raise RuntimeError(f"{self.name} simulated failure")
        content = f"[{self.name}/{model}] respuesta a: {(messages[-1].content or '')[:80]}"
        return LLMResponse(content=content, model=model or self._models[0], usage={"total_tokens": 20})

    async def health(self):
        return self._healthy

    async def list_models(self):
        return list(self._models)


class MockToolProvider(BaseProvider):
    def __init__(self):
        self.name = "tool-mock"

    async def chat(self, messages, tools=None, model=None, **kw):
        if tools:
            tc = ToolCall(id="call_1", name=tools[0]["function"]["name"], arguments={"text": "hola"})
            return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="tool-model")
        return LLMResponse(content="final", model="tool-model")

    async def health(self):
        return True

    async def list_models(self):
        return ["tool-model"]


class FinalAnswerProvider(BaseProvider):
    async def chat(self, messages, tools=None, model=None, **kw):
        return LLMResponse(content="Respuesta final de prueba TDAO", model="final", usage={"total_tokens": 10})

    async def health(self):
        return True

    async def list_models(self):
        return ["final"]


class ToolThenFinalProvider(BaseProvider):
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        if self.calls == 1 and tools:
            tc = ToolCall(id="call_1", name=tools[0]["function"]["name"], arguments={"text": "valor de prueba"})
            return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="tool-model")
        return LLMResponse(content="Hecho tras tool", model="final", usage={"total_tokens": 15})

    async def health(self):
        return True

    async def list_models(self):
        return ["tool-model"]


class FailingProvider(BaseProvider):
    async def chat(self, messages, tools=None, model=None, **kw):
        raise RuntimeError("Simulated LLM failure")

    async def health(self):
        return True

    async def list_models(self):
        return ["fail"]


class LoopProvider(BaseProvider):
    async def chat(self, messages, tools=None, model=None, **kw):
        tc = ToolCall(id="call_1", name="echo", arguments={"text": "loop"})
        return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="loop")

    async def health(self):
        return True

    async def list_models(self):
        return ["loop"]


class EmptyProvider(BaseProvider):
    """Modelo que SIEMPRE devuelve respuesta vacía sin tool_calls (caso 'reply vacío')."""
    async def chat(self, messages, tools=None, model=None, **kw):
        return LLMResponse(content="", tool_calls=(), finish_reason="stop", model="empty")

    async def health(self):
        return True

    async def list_models(self):
        return ["empty"]


class SecretProvider(BaseProvider):
    async def chat(self, messages, tools=None, model=None, **kw):
        tc = ToolCall(id="c1", name="read_secret", arguments={"path": ".env"})
        return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="m")

    async def health(self):
        return True

    async def list_models(self):
        return ["m"]
