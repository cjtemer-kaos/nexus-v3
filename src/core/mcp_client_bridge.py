"""
MCPClientBridge - Consume external MCP servers

Allows NEXUS to connect to ANY MCP server and use its tools.
Features:
- Start MCP servers as subprocesses (stdio JSON-RPC)
- Tool discovery and normalization (mcp__{server}__{tool})
- Schema sanitization
- Health monitoring
- Tool execution with timeout

Refs: byo-coding-agent (Go→Python), learn-claude-code, nexus-mcp-server
"""

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus-mcp-client")


@dataclass
class MCPTool:
    """Represents a tool from an MCP server."""
    name: str
    description: str
    input_schema: Dict
    server: str
    full_name: str = ""  # mcp__{server}__{tool}

    def __post_init__(self):
        if not self.full_name:
            self.full_name = f"mcp__{self.server}__{self.name}"


@dataclass
class MCPServer:
    """Configuration for an MCP server."""
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    auto_start: bool = True
    tools: List[MCPTool] = field(default_factory=list)
    process: Optional[subprocess.Popen] = None
    connected: bool = False


class MCPClientBridge:
    """
    Bridge to external MCP servers.
    Manages server lifecycle, tool discovery, and execution.
    """

    def __init__(self, workdir: str = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self._servers: Dict[str, MCPServer] = {}
        self._tools: Dict[str, MCPTool] = {}
        self._request_id = 0
        # In-process fallbacks (odysseus pattern). Map "{server}.{tool}" → async fn.
        # When an external MCP fails (server down, conn broken, timeout), call_tool
        # consults this map before returning an error — keeps capabilities alive
        # when their dedicated MCP is unavailable.
        self._fallbacks: Dict[str, Any] = {}

    # ─── Server Registration ───────────────────────────────────────────

    def register_server(self, name: str, command: str, args: List[str] = None,
                       env: Dict[str, str] = None, auto_start: bool = True) -> MCPServer:
        """Register an MCP server configuration."""
        server = MCPServer(
            name=name,
            command=command,
            args=args or [],
            env=env or {},
            auto_start=auto_start,
        )
        self._servers[name] = server
        logger.info(f"MCP server registered: {name} ({command} {' '.join(args)})")
        return server

    def load_from_file(self, path: "Path | str") -> int:
        """
        Autodiscovery from JSON manifest (Claude Desktop / aden-hive standard
        format). Returns the number of servers added.

        Searched format (matches mcp_servers.json everywhere):

            {
              "mcpServers": {
                "my-server": {
                  "command": "npx",
                  "args": ["-y", "@x/server"],
                  "env": {"API_KEY": "..."},
                  "auto_start": false
                }
              }
            }

        Servers already registered with the same name are SKIPPED — file
        entries do not silently overwrite builtins. Bad entries log a
        warning and are skipped (the manifest as a whole keeps loading).
        """
        import json
        p = Path(path)
        if not p.exists():
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.warning(f"mcp_servers.json at {p} invalid JSON: {e}")
            return 0
        block = data.get("mcpServers") or data.get("servers") or {}
        if not isinstance(block, dict):
            logger.warning(f"mcp_servers.json at {p}: expected 'mcpServers' dict")
            return 0
        added = 0
        for name, cfg in block.items():
            if not isinstance(cfg, dict):
                logger.warning(f"mcp_servers.json: '{name}' is not a dict, skipping")
                continue
            if name in self._servers:
                logger.info(f"mcp_servers.json: '{name}' already registered, skipping")
                continue
            cmd = cfg.get("command")
            if not cmd:
                logger.warning(f"mcp_servers.json: '{name}' missing 'command', skipping")
                continue
            args = cfg.get("args") or []
            env = cfg.get("env") or {}
            auto_start = bool(cfg.get("auto_start", False))
            self.register_server(
                name=str(name), command=str(cmd),
                args=list(args), env=dict(env), auto_start=auto_start,
            )
            added += 1
        if added:
            logger.info(f"MCP autodiscovery: loaded {added} server(s) from {p}")
        return added

    def autodiscover(self) -> int:
        """
        Look for mcp_servers.json in the canonical locations and load them.
        Returns total servers added. Idempotent — same file loaded twice
        adds nothing the second time (existing-name skip).

        Search order (first wins per name):
            1. <workdir>/mcp_servers.json   (project-level)
            2. <workdir>/.nexus/mcp_servers.json
            3. ~/.nexus/mcp_servers.json    (user-level fallback)
        """
        total = 0
        candidates = [
            self.workdir / "mcp_servers.json",
            self.workdir / ".nexus" / "mcp_servers.json",
            Path.home() / ".nexus" / "mcp_servers.json",
        ]
        for c in candidates:
            try:
                total += self.load_from_file(c)
            except Exception as e:
                logger.warning(f"autodiscover at {c} failed: {e}")
        return total

    def register_builtin_servers(self):
        """Register common MCP servers."""
        # GitHub MCP (if available)
        self.register_server(
            name="github",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_TOKEN": ""},  # User must configure
            auto_start=False,
        )

        # Filesystem MCP — use NEXUS_FS_ROOT env var (comma|semicolon separated) or fallback to home+workdir
        fs_root = os.environ.get("NEXUS_FS_ROOT", "")
        if fs_root:
            fs_dirs = [d.strip() for d in fs_root.replace(";", ",").split(",") if d.strip()]
        else:
            fs_dirs = [str(Path.home()), str(self.workdir)]
        self.register_server(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem"] + fs_dirs,
            auto_start=False,
        )

        # SQLite MCP
        self.register_server(
            name="sqlite",
            command="uvx",
            args=["mcp-server-sqlite", "--db-path", str(Path.home() / ".nexus" / "brain" / "cerebro.db")],
            auto_start=False,
        )

        # Puppeteer MCP (web automation)
        self.register_server(
            name="puppeteer",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-puppeteer"],
            auto_start=False,
        )

        # Brave Search MCP (requires BRAVE_SEARCH_API_KEY env)
        self.register_server(
            name="brave-search",
            command="npx",
            args=["-y", "@brave/brave-search-mcp"],
            auto_start=False,
        )

        # Chrome DevTools MCP
        self.register_server(
            name="chrome-devtools",
            command="npx",
            args=["-y", "chrome-devtools-mcp"],
            auto_start=False,
        )

        # Playwright MCP (self-contained browser automation)
        self.register_server(
            name="playwright",
            command="npx",
            args=["-y", "@playwright/mcp@latest"],
            auto_start=False,
        )

        # Agent-Browser MCP (native Rust, LLM-optimized output)
        python_exe = "python"
        import sys
        python_exe = sys.executable
        bridge_path = Path(__file__).parent.parent / "bridges" / "agent_browser_mcp.py"
        self.register_server(
            name="agent-browser",
            command=python_exe,
            args=["-u", str(bridge_path)],
            auto_start=False,
        )

    # ─── Server Lifecycle ──────────────────────────────────────────────

    async def start_server(self, name: str) -> bool:
        """Start an MCP server and discover its tools."""
        server = self._servers.get(name)
        if not server:
            logger.error(f"MCP server not found: {name}")
            return False

        if server.connected:
            logger.info(f"MCP server {name} already connected")
            return True

        logger.info(f"Starting MCP server: {name}")

        try:
            env = {**dict(__import__("os").environ), **server.env}
            server.process = subprocess.Popen(
                [server.command] + server.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(self.workdir),
            )
            logger.info(f"MCP server {name} started (PID: {server.process.pid})")

            # Discover tools
            tools = await self._discover_tools(server)
            server.tools = tools
            server.connected = True

            for tool in tools:
                self._tools[tool.full_name] = tool

            logger.info(f"MCP server {name}: {len(tools)} tools discovered")
            return True

        except Exception as e:
            logger.error(f"Failed to start MCP server {name}: {e}")
            return False

    async def stop_server(self, name: str):
        """Stop an MCP server."""
        server = self._servers.get(name)
        if server and server.process:
            logger.info(f"Stopping MCP server: {name}")
            try:
                server.process.terminate()
                server.process.wait(timeout=5)
            except Exception:
                server.process.kill()
            server.connected = False
            server.process = None

            # Remove tools
            for tool in server.tools:
                self._tools.pop(tool.full_name, None)
            server.tools = []

    async def start_server_if_needed(self, name: str) -> bool:
        """Start a server only if it's not already connected. Safe to call repeatedly."""
        server = self._servers.get(name)
        if not server:
            logger.warning(f"MCP server not found: {name}")
            return False
        if server.connected:
            return True
        return await self.start_server(name)

    async def start_all(self) -> Dict[str, bool]:
        """Start every server flagged auto_start. Returns {name: ok} so the
        caller can log/emit per-server outcomes. Per-server exceptions are
        caught — one failure never blocks the rest."""
        results: Dict[str, bool] = {}
        for name, server in self._servers.items():
            if not getattr(server, "auto_start", False):
                continue
            try:
                ok = await self.start_server(name)
            except Exception as e:
                logger.error(f"start_all: {name} raised {type(e).__name__}: {e}")
                ok = False
            results[name] = ok
            try:
                from src.observability.event_stream import emit, EventType
                emit(
                    EventType.MCP_SERVER_STARTED if ok else EventType.MCP_SERVER_FAILED,
                    data={"server": name, "via": "start_all"},
                    source="mcp_client_bridge",
                )
            except Exception:
                pass
        return results

    async def stop_all(self):
        """Stop all servers."""
        for name in list(self._servers.keys()):
            await self.stop_server(name)

    async def health_probe(self, attempt_restart: bool = False) -> Dict[str, Dict]:
        """Probe every registered server. Returns per-server health rows:
            {name: {connected, process_alive, tool_count, restarted}}

        When attempt_restart=True, dead-but-was-connected servers are
        relaunched once. Emits MCP_SERVER_FAILED on detected death and
        MCP_SERVER_STARTED on successful restart.

        Cheap to call repeatedly — does no JSON-RPC, just inspects the
        existing subprocess.poll() status (no IPC overhead, no risk of
        timing out on a stuck server).
        """
        report: Dict[str, Dict] = {}
        for name, server in self._servers.items():
            row = {
                "connected": bool(server.connected),
                "process_alive": False,
                "tool_count": len(server.tools or []),
                "restarted": False,
            }
            proc = server.process
            if proc is not None:
                rc = proc.poll()  # None == still running
                row["process_alive"] = (rc is None)
                if rc is not None and server.connected:
                    # Server claimed connected but process died — that's a
                    # crash. Mark disconnected so call_tool fallback kicks in.
                    logger.warning(f"MCP {name} process exited (rc={rc}); marking disconnected")
                    server.connected = False
                    server.process = None
                    server.tools = []
                    row["connected"] = False
                    try:
                        from src.observability.event_stream import emit, EventType
                        emit(EventType.MCP_SERVER_FAILED,
                             data={"server": name, "rc": rc, "detected_by": "health_probe"},
                             source="mcp_client_bridge")
                    except Exception:
                        pass
                    if attempt_restart and getattr(server, "auto_start", False):
                        ok = await self.start_server(name)
                        row["restarted"] = ok
                        if ok:
                            try:
                                from src.observability.event_stream import emit, EventType
                                emit(EventType.MCP_SERVER_STARTED,
                                     data={"server": name, "after": "health_probe_restart"},
                                     source="mcp_client_bridge")
                            except Exception:
                                pass
            report[name] = row
        return report

    # ─── Tool Discovery ────────────────────────────────────────────────

    async def _discover_tools(self, server: MCPServer) -> List[MCPTool]:
        """Discover tools from an MCP server via stdio JSON-RPC."""
        if not server.process or not server.process.stdin:
            return []

        try:
            # Send initialize request
            init_request = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "SuperNEXUS", "version": "2.0.0"},
                },
            }
            await self._send_request(server, init_request)

            # Send initialized notification
            initialized = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
            await self._send_notification(server, initialized)

            # List tools
            tools_request = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/list",
                "params": {},
            }
            response = await self._send_request(server, tools_request)

            if response and "result" in response:
                tools_data = response["result"].get("tools", [])
                tools = []
                for t in tools_data:
                    tool = MCPTool(
                        name=t["name"],
                        description=t.get("description", ""),
                        input_schema=t.get("inputSchema", {}),
                        server=server.name,
                    )
                    tools.append(tool)
                return tools

        except Exception as e:
            logger.warning(f"Tool discovery failed for {server.name}: {e}")

        return []

    async def _send_request(self, server: MCPServer, request: Dict, timeout: float = 10.0) -> Optional[Dict]:
        """Send a JSON-RPC request and read response."""
        if not server.process or not server.process.stdin or not server.process.stdout:
            return None

        # Write request
        data = json.dumps(request) + "\n"
        server.process.stdin.write(data.encode())
        server.process.stdin.flush()

        # Read response (with timeout)
        import select
        if platform.system() != "Windows":
            ready, _, _ = select.select([server.process.stdout], [], [], timeout)
            if not ready:
                return None

        # Read line
        line = server.process.stdout.readline()
        if line:
            try:
                return json.loads(line.decode())
            except json.JSONDecodeError:
                return None
        return None

    async def _send_notification(self, server: MCPServer, notification: Dict):
        """Send a JSON-RPC notification (no response expected)."""
        if not server.process or not server.process.stdin:
            return
        data = json.dumps(notification) + "\n"
        server.process.stdin.write(data.encode())
        server.process.stdin.flush()

    # ─── Tool Execution ────────────────────────────────────────────────

    def register_fallback(self, server: str, tool: str, fn):
        """Register an in-process async fallback for `server.tool`. Activates
        only when the external MCP fails (server down / start fails / request
        error). Idempotent — re-registering same key replaces the handler.

        Pattern (odysseus): keeps capabilities alive when their dedicated MCP
        is unavailable, instead of returning a dead-end error to the model.
        """
        key = f"{server}.{tool}"
        self._fallbacks[key] = fn
        logger.info(f"MCP fallback registered: {key}")

    async def _try_fallback(self, server: str, tool: str, arguments: Dict,
                            reason: str) -> Optional[Dict]:
        """Look up and invoke an in-process fallback. Returns the wrapped
        result dict on hit, None on miss. Emits MCP_SERVER_FAILED on hit so
        observability sees the degradation."""
        key = f"{server}.{tool}"
        fn = self._fallbacks.get(key)
        if not fn:
            return None
        logger.warning(f"MCP fallback ENGAGED for {key} (reason: {reason})")
        try:
            from src.observability.event_stream import emit, EventType
            emit(EventType.MCP_SERVER_FAILED,
                 data={"server": server, "tool": tool, "reason": reason,
                       "fallback": "in_process"},
                 source="mcp_client_bridge")
        except Exception:
            pass
        try:
            res = await fn(arguments)
            return {"success": True, "result": res, "via": "fallback"}
        except Exception as e:
            logger.error(f"MCP fallback {key} itself failed: {e}")
            return {"error": f"fallback for {key} failed: {e}", "via": "fallback"}

    async def call_tool(self, tool_name: str, arguments: Dict) -> Dict:
        """
        Call an MCP tool by its full name (mcp__{server}__{tool}).
        Returns the tool result. Falls back to in-process handler if the
        external server is unavailable AND a fallback is registered.
        """
        tool = self._tools.get(tool_name)
        if not tool:
            # Tool not in registry — try fallback by parsing the name shape.
            # Accept "mcp__SERVER__TOOL" or "SERVER.TOOL".
            server_guess, tool_guess = None, None
            if tool_name.startswith("mcp__"):
                parts = tool_name[5:].split("__", 1)
                if len(parts) == 2:
                    server_guess, tool_guess = parts
            elif "." in tool_name:
                server_guess, tool_guess = tool_name.split(".", 1)
            if server_guess and tool_guess:
                fb = await self._try_fallback(server_guess, tool_guess, arguments, "tool_not_found")
                if fb is not None:
                    return fb
            return {"error": f"Tool not found: {tool_name}", "available": list(self._tools.keys())}

        server = self._servers.get(tool.server)
        if not server:
            fb = await self._try_fallback(tool.server, tool.name, arguments, "server_not_registered")
            if fb is not None:
                return fb
            return {"error": f"Server {tool.server} not found"}
        if not server.connected:
            ok = await self.start_server_if_needed(tool.server)
            if not ok:
                fb = await self._try_fallback(tool.server, tool.name, arguments, "server_start_failed")
                if fb is not None:
                    return fb
                return {"error": f"Server {tool.server} could not be started"}

        try:
            request = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {
                    "name": tool.name,
                    "arguments": arguments,
                },
            }
            response = await self._send_request(server, request, timeout=30.0)

            if response and "result" in response:
                return {"success": True, "result": response["result"]}
            elif response and "error" in response:
                fb = await self._try_fallback(tool.server, tool.name, arguments, "remote_error")
                if fb is not None:
                    return fb
                return {"error": response["error"].get("message", "Unknown error")}
            else:
                fb = await self._try_fallback(tool.server, tool.name, arguments, "no_response")
                if fb is not None:
                    return fb
                return {"error": "No response from server"}

        except Exception as e:
            fb = await self._try_fallback(tool.server, tool.name, arguments, f"exception:{type(e).__name__}")
            if fb is not None:
                return fb
            return {"error": str(e)}

    # ─── Tool Listing ──────────────────────────────────────────────────

    def list_tools(self) -> List[Dict]:
        """List all available MCP tools."""
        return [
            {
                "name": tool.full_name,
                "description": tool.description,
                "server": tool.server,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    def get_tools_for_server(self, server_name: str) -> List[Dict]:
        """List tools for a specific server."""
        return [
            {
                "name": tool.full_name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
            if tool.server == server_name
        ]

    # ─── Helpers ───────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def get_status(self) -> Dict:
        return {
            "servers": {
                name: {
                    "connected": server.connected,
                    "tools": len(server.tools),
                    "command": server.command,
                }
                for name, server in self._servers.items()
            },
            "total_tools": len(self._tools),
        }

    def __del__(self):
        """Cleanup on deletion."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.stop_all())
            else:
                loop.run_until_complete(self.stop_all())
        except Exception:
            pass


# Platform import for _send_request
import platform
