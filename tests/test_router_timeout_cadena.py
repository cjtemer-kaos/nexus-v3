"""Timeout de transporte: se reintenta el mismo modelo y NO se recorre la cadena.

Caso real (2026-10-28, `data/backend.log`): un pedido estancado sin primer token
recorrio deepseek-v4.1-flash -> deepseek-v4-pro -> glm-5.1 y se comio 213 s
(el 45% de la espera al modelo de toda la corrida) cuando el mismo modelo
respondia bien en el intento siguiente. Cambiar de modelo no arregla un upstream
caido: solo agrega otro minuto de espera por candidato.
"""
from __future__ import annotations

import asyncio

import httpx

from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse
from src.core.provider_router import ModelRoute, ProviderRouter


class ProveedorQueSeEstanca(BaseProvider):
    """Nunca entrega ni el primer token."""

    def __init__(self, nombre: str):
        self.nombre = nombre
        self.llamadas = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        self.llamadas += 1
        raise httpx.ReadTimeout("sin primer token")

    async def stream(self, messages, tools=None, model=None, **kw):  # type: ignore[override]
        self.llamadas += 1
        raise httpx.ReadTimeout("sin primer token")
        yield  # pragma: no cover

    async def health(self):
        return True

    async def list_models(self):
        return ["m1"]


class ProveedorSano(BaseProvider):
    def __init__(self):
        self.llamadas = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        self.llamadas += 1
        return LLMResponse(content="respuesta del sano", model=model or "m2")

    async def stream(self, messages, tools=None, model=None, **kw):  # type: ignore[override]
        self.llamadas += 1
        yield {"type": "delta", "text": "respuesta del sano"}

    async def health(self):
        return True

    async def list_models(self):
        return ["m2"]


def _router():
    r = ProviderRouter()
    estancado = ProveedorQueSeEstanca("estancado")
    sano = ProveedorSano()
    r.register_provider("estancado", estancado)
    r.register_provider("sano", sano)
    r.register_model(ModelRoute(id="m1", provider_name="estancado"))
    r.register_model(ModelRoute(id="m2", provider_name="sano"))
    r.fallback_order = ["estancado", "sano"]
    return r, estancado, sano


def test_chat_no_recorre_la_cadena_tras_timeout():
    async def run():
        router, estancado, sano = _router()
        msgs = [LLMMessage(role="user", content="hola")]
        try:
            await router.chat(msgs, model="m1")
            return "sin error", estancado.llamadas, sano.llamadas
        except Exception:
            return "error", estancado.llamadas, sano.llamadas

    resultado, intentos_estancado, intentos_sano = asyncio.run(run())
    # Se corta con error (no inventa respuesta)...
    assert resultado == "error"
    # ...despues de reintentar UNA vez el mismo modelo, no toda la cadena.
    assert intentos_estancado == 2
    assert intentos_sano == 0


def test_stream_no_recorre_la_cadena_tras_timeout():
    async def run():
        router, estancado, sano = _router()
        msgs = [LLMMessage(role="user", content="hola")]
        eventos = []
        try:
            async for ev in router.stream(msgs, model="m1"):
                eventos.append(ev)
        except Exception:
            pass
        return eventos, estancado.llamadas, sano.llamadas

    eventos, intentos_estancado, intentos_sano = asyncio.run(run())
    assert eventos == []
    assert intentos_estancado == 2
    assert intentos_sano == 0
