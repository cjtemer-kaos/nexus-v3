"""Sección [2] Tool registry and Scope guard (paridad run_tests.py)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from src.core.hooks_engine import HooksEngine
from src.core.scope import Scope, ScopeDenied, ScopeConfig
from src.core.tool_registry import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent


def make_scope():
    return Scope(
        ScopeConfig(
            allowed_roots=("./workspace", "./data"),
            deny_patterns=("**/.env", "**/*.key"),
        ),
        project_root=ROOT,
    )


def test_register_function():
    reg = ToolRegistry()

    def add(a: int, b: int):
        return a + b

    reg.register_function(add, name="add", description="suma", category="local")
    count = reg.count()
    has_add = reg.has("add")
    assert count >= 1
    assert has_add


def test_tool_decorator():
    reg = ToolRegistry()

    @reg.tool(description="echo tool")
    def echo(text: str):
        return text

    assert reg.has("echo")


def test_to_openai_tools():
    reg = ToolRegistry()
    reg.register_function(lambda a, b: a + b, name="add", description="suma")
    tools_oai = reg.to_openai_tools()
    assert len(tools_oai) >= 1
    tools_oai[0]["function"]["name"]


def test_categories():
    reg = ToolRegistry()
    reg.register_function(lambda a, b: a + b, name="add", description="suma", category="local")
    cats = reg.categories()
    assert isinstance(cats, (list, dict, tuple, set))


def test_load_skill_bundle():
    reg = ToolRegistry()
    reg.load_skill_bundle(
        "test-skill",
        [{"name": "skill_fn", "description": "demo", "parameters": {"type": "object", "properties": {}}}],
    )
    assert reg.has("skill_fn")
    assert reg.get("skill_fn").category


def test_load_mcp_tools():
    reg = ToolRegistry()
    reg.load_mcp_tools("mcp-server", [{"name": "mcp_fn", "description": "mcp demo", "parameters": {}}])
    assert reg.has("mcp_fn")


def test_scope_check_path_allowed():
    scope = make_scope()
    scope.check_path("workspace/file.txt")


def test_scope_deny_env():
    scope = make_scope()
    with pytest.raises(ScopeDenied):
        scope.check_path(".env")


def test_scope_traversal_block():
    scope = make_scope()
    with pytest.raises(ScopeDenied):
        scope.check_path("workspace/../../etc/passwd")


def test_scope_deny_brain_db_shm():
    scope = make_scope()
    with pytest.raises(ScopeDenied):
        scope.check_path("brain/cerebro.db-shm")


def test_scope_is_path_allowed():
    scope = make_scope()
    assert scope.is_path_allowed("workspace/ok.txt") == True


def test_scope_network_gate_denied():
    scope_no_net = Scope(
        ScopeConfig(allowed_roots=("./workspace",), allow_network=False),
        project_root=ROOT,
    )
    with pytest.raises(ScopeDenied):
        scope_no_net.check_tool("web_fetch", {"url": "https://example.com"})


def test_scope_network_gate_allowed():
    scope_net = Scope(
        ScopeConfig(allowed_roots=("./workspace",), allow_network=True),
        project_root=ROOT,
    )
    scope_net.check_tool("web_fetch", {"url": "https://example.com"})


async def _dispatch_suite():
    scope = make_scope()

    def read_file(path: str):
        return f"contenido de {path}"

    reg2 = ToolRegistry()
    reg2.register_function(read_file, name="read_file", description="lee archivo")

    # dispatch success
    res = await reg2.dispatch("read_file", {"path": "workspace/foo.txt"}, scope=scope)
    assert res == "contenido de workspace/foo.txt"

    # dispatch denied
    with pytest.raises(ScopeDenied):
        await reg2.dispatch("read_file", {"path": ".env"}, scope=scope)

    # metrics: scope-denied calls are rejected BEFORE metrics (guard before t0)
    entry = reg2.get("read_file")
    assert entry.call_count == 1 and entry.success_count == 1

    # async handler
    async def async_echo(text: str):
        return f"async:{text}"

    reg2.register_function(async_echo, name="async_echo")
    res2 = await reg2.dispatch("async_echo", {"text": "hello"})
    assert res2 == "async:hello"

    # hooks pre/post
    hooks = HooksEngine(config={"security_gate": True, "phases": ["pre_tool_use", "post_tool_use"]})
    events = []

    def h_pre(phase, payload):
        events.append(phase)

    hooks.on("pre_tool_use", h_pre)
    hooks.on("post_tool_use", h_pre)
    await reg2.dispatch("async_echo", {"text": "hook-test"}, hooks=hooks)
    assert "pre_tool_use" in events and "post_tool_use" in events

    # stats
    st = reg2.stats()
    assert st["total_tools"] >= 2


def test_dispatch_suite():
    asyncio.run(_dispatch_suite())
