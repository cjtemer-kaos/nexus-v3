"""Provider Ollama: errores que no pierden el motivo + cliente recreable.

Ambos salieron del diagnostico del 2026-10-26 (turnos de 100-165 s):
- raise_for_status() borraba el body, asi que "exceeds the available context size"
  era indistinguible de "modelo roto" y el router recorria todo el catalogo.
- un router retirado se cierra a los 30 s; si un run largo lo usaba, todas sus
  llamadas morian con "Cannot send a request, as the client has been closed".
"""
from __future__ import annotations

import asyncio

import httpx

from src.core.provider_base import LLMMessage
from src.providers.ollama_provider import OllamaProvider


def _with_transport(handler) -> OllamaProvider:
    p = OllamaProvider(base_url="http://test", timeout_s=5)
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
    return p


def test_error_400_conserva_el_motivo_del_body():
    def handler(request):
        return httpx.Response(400, json={
            "error": "request (10132 tokens) exceeds the available context size "
                     "(8192 tokens), try increasing it",
        })

    p = _with_transport(handler)
    captured = ""
    try:
        asyncio.run(p.chat([LLMMessage(role="user", content="hola")], model="qwen3-vl-8b"))
        raise AssertionError("deberia haber lanzado")
    except RuntimeError as exc:
        captured = str(exc)
        assert "exceeds the available context size" in captured
        assert "400" in captured

    from src.core.provider_router import es_overflow_de_contexto

    assert es_overflow_de_contexto(captured)


def test_cliente_cerrado_se_recrea():
    p = OllamaProvider(base_url="http://test", timeout_s=5)
    old = p._client
    asyncio.run(old.aclose())
    assert old.is_closed

    p._ensure_client()
    assert p._client is not old
    assert not p._client.is_closed
    asyncio.run(p.close())


def test_cliente_vivo_no_se_recrea():
    p = OllamaProvider(base_url="http://test", timeout_s=5)
    before = p._client
    p._ensure_client()
    assert p._client is before
    asyncio.run(p.close())
