"""Tests de la pasarela LLM: costos, cadena de failover y barrera de estado de tools."""
import asyncio

import pytest

from src.core.llm_provider import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    estimate_cost_usd,
)


@pytest.fixture()
def provider():
    p = LLMProvider()
    yield p
    asyncio.run(p.close())


def _req(provider="ollama", messages=None):
    return LLMRequest(
        messages=messages or [{"role": "user", "content": "hola"}],
        model="modelo-test",
        provider=provider,
    )


# -- costos ---------------------------------------------------------------

def test_costo_de_modelo_conocido():
    assert estimate_cost_usd("gpt-4o-mini", 1_000_000, 0) == 0.15
    assert estimate_cost_usd("gpt-4o-mini", 0, 1_000_000) == 0.6


def test_costo_usa_el_prefijo_mas_especifico():
    # "gpt-4o-mini-2024" debe matchear gpt-4o-mini, no gpt-4o
    assert estimate_cost_usd("gpt-4o-mini-2024-07-18", 1_000_000, 0) == 0.15


def test_costo_desconocido_o_local_es_cero():
    assert estimate_cost_usd("modelo-que-no-existe", 5000, 5000) == 0.0
    assert estimate_cost_usd("ollama/qwen2.5", 5000, 5000) == 0.0
    assert estimate_cost_usd("", 10, 10) == 0.0


# -- failover -------------------------------------------------------------

def test_cadena_de_failover_arranca_por_el_principal(provider):
    chain = provider._get_failover_chain("gemini")
    assert chain[0] == "gemini"
    assert len(chain) == 5
    assert set(chain) == {"ollama", "openrouter", "gemini", "anthropic", "openai"}
    assert provider._get_failover_chain("openai")[0] == "openai"


def test_chat_hace_failover_y_marca_el_primero_como_fallido(provider, monkeypatch):
    llamadas = []

    async def fake_call_provider(**kwargs):
        llamadas.append(kwargs)
        if kwargs["provider"] == "ollama":
            raise RuntimeError("ollama caido")
        return LLMResponse(
            content="ok", model=kwargs["model"], provider=kwargs["provider"],
            tokens_used=10, tokens_prompt=6, tokens_completion=4,
        )

    monkeypatch.setattr(provider, "_call_provider", fake_call_provider)

    resp = asyncio.run(provider.chat(_req()))

    assert resp.provider == "openrouter"
    assert resp.content == "ok"
    assert llamadas[0]["provider"] == "ollama"
    assert llamadas[1]["provider"] == "openrouter"
    stats = provider.get_stats()
    assert stats["failovers"] == 1
    assert stats["successful_requests"] == 1
    assert stats["provider_stats"]["ollama"]["failed"] == 1
    assert stats["provider_stats"]["openrouter"]["success"] == 1


def test_chat_si_todos_fallan_lanza_y_cuenta(provider, monkeypatch):
    async def siempre_falla(**kwargs):
        raise RuntimeError("sin red")

    monkeypatch.setattr(provider, "_call_provider", siempre_falla)

    with pytest.raises(RuntimeError, match="All providers failed"):
        asyncio.run(provider.chat(_req()))

    stats = provider.get_stats()
    assert stats["failed_requests"] == 1
    assert stats["successful_requests"] == 0
    assert len(stats["provider_stats"]) == 5


# -- barrera de estado de tools (failover) --------------------------------

def test_barrera_elimina_cadenas_de_tools_y_agrega_aviso(provider):
    mensajes = [
        {"role": "system", "content": "sos un agente"},
        {"role": "user", "content": [
            {"type": "text", "text": "abri el navegador"},
            {"type": "tool_use", "id": "t1", "name": "browser_open"},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
    ]

    out = provider._inject_tool_state_barrier(mensajes)

    assert out[0] == mensajes[0]
    assert out[1]["content"] == [{"type": "text", "text": "abri el navegador"}]
    assert out[-1]["role"] == "user"
    assert "[provider-failover]" in out[-1]["content"]
    assert "MUST be treated as unknown" in out[-1]["content"]


def test_barrera_sin_tools_devuelve_lo_mismo(provider):
    mensajes = [{"role": "user", "content": "hola"}]
    assert provider._inject_tool_state_barrier(mensajes) is mensajes
    assert provider._inject_tool_state_barrier([]) == []


def test_el_failover_entrega_la_barrera_al_segundo_proveedor(provider, monkeypatch):
    recibidos = []

    async def fake_call_provider(**kwargs):
        recibidos.append(kwargs["messages"])
        if kwargs["provider"] == "ollama":
            raise RuntimeError("timeout")
        return LLMResponse(content="ok", model=kwargs["model"], provider=kwargs["provider"])

    monkeypatch.setattr(provider, "_call_provider", fake_call_provider)
    mensajes = [
        {"role": "user", "content": [
            {"type": "text", "text": "estado previo"},
            {"type": "tool_use", "id": "abc"},
        ]},
    ]

    asyncio.run(provider.chat(_req(messages=mensajes)))

    assert len(recibidos) == 2
    segundo = recibidos[1]
    assert any(
        isinstance(m.get("content"), str) and "[provider-failover]" in m["content"]
        for m in segundo
    )
    assert not any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_use" for b in m["content"] if isinstance(b, dict))
        for m in segundo
    )
