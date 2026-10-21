"""Servidor MCP (Model Context Protocol) sobre stdio que expone las tools de nexus-v3.

Permite que clientes MCP (opencode, Claude Desktop, Cursor, etc.) usen el harness
de nexus-v3 como servidor de herramientas. Reusa el ToolRegistry + scope.

Uso:
    python -m src.mcp_server            (cwd = raiz de nexus-v3)

Config en un cliente MCP:
    {"mcpServers": {"nexus-v3": {
        "command": "<nexus-v3>/.venv-ft314/Scripts/python.exe",
        "args": ["-m", "src.mcp_server"],
        "cwd": "<nexus-v3>"}}}
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

PROTOCOL_VERSION = "2024-11-05"


def _log(*a) -> None:
    print("[nexus-mcp]", *a, file=sys.stderr, flush=True)


class NexusMCPServer:
    def __init__(self) -> None:
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())
        from src.api.server import (
            _build_approval_context,
            _build_registry,
            _build_router_from_config,
            _build_scope,
            _load_config_safe,
        )
        cfg = _load_config_safe()
        self.scope = _build_scope(cfg)
        self.registry = _build_registry(self.scope, "mcp", cfg, router=_build_router_from_config(cfg))
        # Servidor MCP entrante: no hay UI que apruebe -> fail-closed.
        self.approval = _build_approval_context(
            cfg, "mcp", source="mcp-server", interactive=False, policy="never")
        self.registry.approval = self.approval

    def tools(self) -> list[dict]:
        out = []
        for t in self.registry.list():
            out.append({
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.parameters or {"type": "object", "properties": {}, "additionalProperties": True},
            })
        return out

    async def call(self, name: str, arguments: dict) -> str:
        res = await self.registry.dispatch(name, arguments or {}, scope=self.scope,
                                           approval=self.approval, session_id="mcp")
        return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)

    async def handle(self, msg: dict) -> dict | None:
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "nexus-v3", "version": "3.0.0"},
            }}
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": self.tools()}}
        if method == "tools/call":
            params = msg.get("params") or {}
            try:
                text = await self.call(params.get("name", ""), params.get("arguments") or {})
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": text}], "isError": False}}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    server = NexusMCPServer()
    _log("listo; tools:", len(server.tools()))
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        try:
            resp = asyncio.run(server.handle(msg))
        except Exception as e:
            _log("handler error:", e)
            continue
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
