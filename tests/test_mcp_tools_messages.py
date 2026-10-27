"""mcp_list / mcp_call: cuando el nombre esta mal, decir cuales existen.

Medido con el registry real:

    mcp_list(server="no-existe-xyz")  -> KeyError: servidor MCP desconocido
    mcp_call(server="no-existe-xyz")  -> KeyError idem
    mcp_call(server="playwright", tool="no-existe-tool")
        -> {"isError": true, "content": "Tool ... not found"}

Un nombre mal escrito dejaba al modelo sin la lista de servidores validos, y una
tool mal escrita sin la lista de tools (playwright tiene 45): adivinar cuesta
turnos. Ahora el error trae los nombres, y el `isError` trae `available_tools`.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _CliFalso:
    servidores = [
        {"name": "playwright", "kind": "npx"},
        {"name": "context7", "kind": "npx"},
    ]
    tools = {"playwright": [{"name": "browser_close"}, {"name": "browser_snapshot"}]}

    def list_servers(self):
        return self.servidores

    def list_tools(self, server):
        if server not in self.tools:
            raise KeyError(f"servidor MCP desconocido: {server}")
        return self.tools[server]

    def call(self, server, tool, args):
        if server not in self.tools:
            raise KeyError(f"servidor MCP desconocido: {server}")
        if tool not in [t["name"] for t in self.tools[server]]:
            return {"server": server, "tool": tool, "isError": True,
                    "content": f'### Error\nTool "{tool}" not found'}
        return {"server": server, "tool": tool, "isError": False, "content": "ok"}


@pytest.fixture()
def mcp(monkeypatch):
    from src.api import server as S
    from src.core import mcp_client

    monkeypatch.setattr(mcp_client, "get_mcp_client", lambda *a, **k: _CliFalso())

    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, f"test-mcp-{uuid.uuid4().hex[:8]}", cfg)

    def usar(name, args=None):
        return asyncio.run(reg.dispatch(name, args or {}, scope=scope, hooks=None))

    return usar


def test_mcp_list_sin_server_lista_los_que_hay(mcp):
    out = json.loads(mcp("mcp_list"))

    assert [s["name"] for s in out["servers"]] == ["playwright", "context7"]


def test_mcp_list_server_desconocido_dice_cuales_existen(mcp):
    with pytest.raises(ValueError) as e:
        mcp("mcp_list", {"server": "no-existe-xyz"})

    msg = str(e.value)
    assert "desconocido" in msg
    assert "playwright" in msg and "context7" in msg


def test_mcp_call_server_desconocido_tambien_lista(mcp):
    with pytest.raises(ValueError) as e:
        mcp("mcp_call", {"server": "no-existe-xyz", "tool": "x"})

    assert "context7" in str(e.value)


def test_mcp_call_tool_inexistente_trae_las_validas(mcp):
    out = json.loads(mcp("mcp_call", {"server": "playwright", "tool": "no-existe-tool"}))

    assert out["isError"] is True
    assert out["available_tools"] == ["browser_close", "browser_snapshot"]


def test_mcp_call_ok_no_agrega_ruido(mcp):
    out = json.loads(mcp("mcp_call", {"server": "playwright", "tool": "browser_close"}))

    assert out["isError"] is False
    assert "available_tools" not in out
