"""MCP client — conecta servidores stdio del bundle portable (npx/uvx) y expone sus tools.

Just-in-time: el servidor se lanza la primera vez que se usa y se mantiene vivo en un
loop de fondo propio. Nada hardcodeado: los servidores vienen de data/mcp_bundle/manifest.json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("nexus-mcp-client")

_manifest_cache: dict[str, list[dict]] = {}

_SENSITIVE_ENV = __import__("re").compile(r"KEY|PASSWORD|SECRET|TOKEN|CREDENTIAL|AUTH", __import__("re").I)


def _scrubbed_env(extra: dict | None = None) -> dict:
    """Env para subprocesos MCP: NO hereda credenciales (patrón deepseek scrubbedParentEnv).

    Los paquetes npx/uvx de terceros NO deben ver OPENCODE_API_KEY, GITHUB_TOKEN, etc.
    Se conservan las vars normales (PATH, HOME, locale) + las declaradas por el manifest.
    """
    out = {k: v for k, v in os.environ.items() if not _SENSITIVE_ENV.search(k)}
    if extra:
        out.update({str(k): str(v) for k, v in extra.items()})
    return out


def load_manifest(project_root: str | Path) -> list[dict]:
    key = str(project_root)
    if key in _manifest_cache:
        return _manifest_cache[key]
    p = Path(project_root) / "data" / "mcp_bundle" / "manifest.json"
    entries: list[dict] = []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = [e for e in data.get("entries", []) if e.get("command")]
    except Exception as e:
        logger.warning("mcp manifest no legible en %s: %s", p, e)
    _manifest_cache[key] = entries
    return entries


class MCPClient:
    def __init__(self, project_root: str | Path):
        self.root = str(project_root)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._sessions: dict[str, tuple[Any, Any]] = {}
        self._tools: dict[str, list[dict]] = {}
        self._lock = threading.RLock()

    def _ensure_loop(self) -> None:
        if self._loop is not None:
            return
        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._ready.set()
            self._loop.run_forever()
        self._thread = threading.Thread(target=_run, name="mcp-client-loop", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)

    def _submit(self, coro, timeout: float = 90.0):
        self._ensure_loop()
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    def _entry(self, server: str) -> dict | None:
        for e in load_manifest(self.root):
            if e.get("name") == server:
                return e
        return None

    def list_servers(self) -> list[dict]:
        return [
            {
                "name": e.get("name"),
                "kind": e.get("kind"),
                "scopes": e.get("scopes", []),
                "zero_config": e.get("zero_config", False),
                "needs_account": e.get("needs_account", False),
                "env_required": e.get("env_required", []),
                "enabled_default": e.get("enabled_default", False),
            }
            for e in load_manifest(self.root)
        ]

    async def _connect(self, entry: dict):
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        name = entry["name"]
        if name in self._sessions:
            return self._sessions[name]
        cmd = entry.get("command") or []
        if not cmd:
            raise ValueError(f"{name}: sin command")
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            params = StdioServerParameters(
                command=cmd[0], args=list(cmd[1:]),
                env=_scrubbed_env(entry.get("env")),
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = await session.list_tools()
            self._tools[name] = [
                {
                    "name": t.name,
                    "description": getattr(t, "description", "") or "",
                    "input_schema": getattr(t, "inputSchema", {}) or {},
                }
                for t in tools.tools
            ]
            self._sessions[name] = (session, stack)
            return self._sessions[name]
        except Exception:
            try:
                await stack.aclose()
            except Exception:
                pass
            raise

    def list_tools(self, server: str, timeout: float = 90.0) -> list[dict]:
        entry = self._entry(server)
        if entry is None:
            raise KeyError(f"servidor MCP desconocido: {server}")
        self._submit(self._connect(entry), timeout)
        return self._tools.get(server, [])

    def call(self, server: str, tool: str, arguments: dict | None = None, timeout: float = 180.0) -> dict:
        entry = self._entry(server)
        if entry is None:
            raise KeyError(f"servidor MCP desconocido: {server}")
        self._submit(self._connect(entry), timeout)
        session = self._sessions[server][0]

        async def _call():
            return await session.call_tool(tool, arguments or {})

        res = self._submit(_call(), timeout)
        parts: list[str] = []
        for c in getattr(res, "content", []) or []:
            t = getattr(c, "text", None)
            parts.append(t if t is not None else str(c))
        return {
            "server": server,
            "tool": tool,
            "isError": bool(getattr(res, "isError", False)),
            "content": "\n".join(parts),
        }

    def close_all(self) -> None:
        for name, (_session, stack) in list(self._sessions.items()):
            try:
                self._submit(stack.aclose(), 10.0)
            except Exception:
                pass
        self._sessions.clear()
        self._tools.clear()


_client: MCPClient | None = None
_client_lock = threading.Lock()


def get_mcp_client(project_root: str | Path) -> MCPClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = MCPClient(project_root)
        return _client
