"""/v1/chat/completions debe respetar `tools` (compat OpenAI real).

Antes el endpoint descartaba `tools` en silencio y respondia texto con
finish_reason="stop", asi que un cliente OpenAI concluia que el modelo no
quiso usar herramientas.
"""
from __future__ import annotations

import json

import pytest

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lee un archivo",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


class _StubRouter:
    def __init__(self, tool_calls=(), content="hola"):
        from src.core.provider_base import LLMResponse, ToolCall

        self.calls = []
        self._resp = LLMResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            finish_reason="tool_calls" if tool_calls else "stop",
            usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            model="qwen2.5-coder:7b",
        )

    async def chat(self, messages, tools=None, model=None, hint=None, **kw):
        self.calls.append({"messages": messages, "tools": tools, "model": model, "hint": hint})
        return self._resp


def _client(monkeypatch, router):
    from fastapi.testclient import TestClient

    from src.api import server as srv

    monkeypatch.setattr(srv, "_get_router", lambda cfg=None: router)
    return TestClient(srv.app)


# --- normalizacion de tools -------------------------------------------------

def test_normaliza_tools_validas():
    from src.api.server import _openai_normalize_tools

    out = _openai_normalize_tools(TOOLS)
    assert out == TOOLS


def test_normaliza_tool_sin_prefijo_function():
    from src.api.server import _openai_normalize_tools

    out = _openai_normalize_tools([{"name": "ping", "parameters": {"type": "object"}}])
    assert out[0]["function"]["name"] == "ping"
    assert out[0]["type"] == "function"


def test_tools_sin_parameters_usa_esquema_vacio():
    from src.api.server import _openai_normalize_tools

    out = _openai_normalize_tools([{"type": "function", "function": {"name": "x"}}])
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


@pytest.mark.parametrize("bad", [
    [{"type": "function", "function": {}}],
    [{"type": "function"}],
    [{"type": "retrieval", "function": {"name": "x"}}],
    [{"type": "function", "function": {"name": "x", "parameters": "no-objeto"}}],
    ["no-soy-objeto"],
])
def test_tools_invalidas_lanzan_valueerror(bad):
    from src.api.server import _openai_normalize_tools

    with pytest.raises(ValueError):
        _openai_normalize_tools(bad)


# --- conversion de mensajes -------------------------------------------------

def test_convierte_assistant_con_tool_calls_y_role_tool():
    from src.api.server import _openai_to_llm_messages

    msgs = _openai_to_llm_messages([
        {"role": "system", "content": "sos un agente"},
        {"role": "user", "content": [{"type": "text", "text": "lei el archivo"}]},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "contenido"},
    ])
    assert [m.role for m in msgs] == ["system", "user", "assistant", "tool"]
    assert msgs[1].content == "lei el archivo"
    assert msgs[2].tool_calls[0].name == "read_file"
    assert msgs[2].tool_calls[0].arguments == {"path": "a.txt"}
    assert msgs[3].tool_call_id == "call_1"
    assert msgs[3].content == "contenido"


def test_arguments_invalidos_no_revientan():
    from src.api.server import _openai_to_llm_messages

    msgs = _openai_to_llm_messages([{
        "role": "assistant",
        "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "{no-json"}}],
    }])
    assert msgs[0].tool_calls[0].arguments == {"_raw": "{no-json"}


# --- endpoint ---------------------------------------------------------------

def test_endpoint_devuelve_tool_calls(monkeypatch):
    from src.core.provider_base import ToolCall

    router = _StubRouter(content="", tool_calls=[ToolCall(id="call_abc", name="read_file",
                                                         arguments={"path": "README.md"})])
    client = _client(monkeypatch, router)
    r = client.post("/v1/chat/completions", json={
        "model": "code",
        "messages": [{"role": "user", "content": "lee README.md"}],
        "tools": TOOLS,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tc = choice["message"]["tool_calls"][0]
    assert tc["id"] == "call_abc"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "read_file"
    assert json.loads(tc["function"]["arguments"]) == {"path": "README.md"}
    assert choice["message"]["content"] is None
    assert r.headers["x-nexus-tools"] == "passthrough"
    # las tools llegaron al provider, no se descartaron
    assert router.calls[0]["tools"] == TOOLS


def test_tool_calls_con_texto_previo_conserva_ambos(monkeypatch):
    from src.core.provider_base import ToolCall

    router = _StubRouter(content="Voy a leerlo.",
                         tool_calls=[ToolCall(id="c1", name="read_file",
                                              arguments={"path": "a"})])
    client = _client(monkeypatch, router)
    r = client.post("/v1/chat/completions", json={
        "model": "code",
        "messages": [{"role": "user", "content": "lee a"}],
        "tools": TOOLS,
    })
    choice = r.json()["choices"][0]
    assert choice["message"]["content"] == "Voy a leerlo."
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "read_file"
    assert choice["finish_reason"] == "tool_calls"


def test_endpoint_sin_tool_calls_devuelve_texto(monkeypatch):
    client = _client(monkeypatch, _StubRouter(content="listo"))
    r = client.post("/v1/chat/completions", json={
        "model": "code",
        "messages": [{"role": "user", "content": "hola"}],
        "tools": TOOLS,
    })
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == "listo"
    assert "tool_calls" not in choice["message"]


def test_tool_choice_none_no_pasa_tools(monkeypatch):
    router = _StubRouter(content="sin tools")
    client = _client(monkeypatch, router)
    r = client.post("/v1/chat/completions", json={
        "model": "code",
        "messages": [{"role": "user", "content": "hola"}],
        "tools": TOOLS,
        "tool_choice": "none",
    })
    assert r.status_code == 200
    # no uso el passthrough: no hay header y el router no recibio las tools del request
    assert "x-nexus-tools" not in r.headers
    assert all(c["tools"] != TOOLS for c in router.calls)


def test_tools_mal_formadas_400(monkeypatch):
    client = _client(monkeypatch, _StubRouter())
    r = client.post("/v1/chat/completions", json={
        "model": "code",
        "messages": [{"role": "user", "content": "hola"}],
        "tools": "no-soy-lista",
    })
    assert r.status_code == 400
    assert "tools" in r.json()["detail"]


def test_stream_con_tools_emite_tool_calls(monkeypatch):
    from src.core.provider_base import ToolCall

    router = _StubRouter(content=None, tool_calls=[
        ToolCall(id="call_s", name="read_file", arguments={"path": "x"})
    ])
    client = _client(monkeypatch, router)
    r = client.post("/v1/chat/completions", json={
        "model": "code",
        "messages": [{"role": "user", "content": "lee x"}],
        "tools": TOOLS,
        "stream": True,
    })
    assert r.status_code == 200
    texto = r.text
    assert "data: [DONE]" in texto
    chunks = [json.loads(l[6:]) for l in texto.splitlines() if l.startswith("data: {")]
    deltas = [c for c in chunks if c["choices"][0]["delta"].get("tool_calls")]
    assert deltas and deltas[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "read_file"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
