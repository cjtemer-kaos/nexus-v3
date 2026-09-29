"""
MCP Chat Bridge - Comunicacion en tiempo real con programas/IAs via MCP

Conecta a MCP servers (stdio, SSE, HTTP) para enviar/recibir ordenes
en tiempo real.
"""

import logging
import os
from typing import Dict, List
from dataclasses import dataclass, field
from enum import Enum
from src.core import nexus_config

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


class MCPTransport(Enum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


@dataclass
class MCPServer:
    """Configuracion de un servidor MCP"""
    name: str
    transport: MCPTransport
    command: str = ""
    args: List[str] = field(default_factory=list)
    url: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    status: str = "disconnected"
    tools: List[Dict] = field(default_factory=list)


class MCPChatBridge:
    """
    Bridge para comunicacion con servidores MCP.
    Soporta stdio, SSE y Streamable HTTP.
    """

    def __init__(self):
        self.servers: Dict[str, MCPServer] = {}
        self._load_known_servers()

    def _load_known_servers(self):
        """Carga servidores MCP conocidos + autodiscovery"""
        self.servers = {
            "nexus_master": MCPServer(
                name="nexus_master",
                transport=MCPTransport.STREAMABLE_HTTP,
                url=nexus_config.get_nexus_url(),
                status="unknown",
            ),
            "openclaw": MCPServer(
                name="openclaw",
                transport=MCPTransport.STREAMABLE_HTTP,
                url="http://127.0.0.1:8765/",
                env={"AUTH_HEADER": f"Bearer {os.getenv('OPENCLAW_MCP_TOKEN', '')}"},
                status="unknown",
            ),
        }
        self._load_discovered_servers()

    def _load_discovered_servers(self):
        try:
            from src.bridges.mcp_autodiscovery import discover_servers
            discovered = discover_servers()
            for srv in discovered:
                name = srv["name"]
                if name in self.servers:
                    continue
                transport_str = srv.get("transport", "streamable_http")
                try:
                    transport = MCPTransport(transport_str)
                except ValueError:
                    logger.warning(f"Unknown transport '{transport_str}' for {name}, skipping")
                    continue
                self.servers[name] = MCPServer(
                    name=name,
                    transport=transport,
                    command=srv.get("command", ""),
                    args=srv.get("args", []),
                    url=srv.get("url", ""),
                    env=srv.get("env", {}),
                    status="discovered",
                )
        except Exception as e:
            logger.warning(f"mcp_autodiscovery: load failed — {e}")

    async def connect(self, server_name: str) -> bool:
        """Conecta a un servidor MCP"""
        if server_name not in self.servers:
            logger.error(f"Server '{server_name}' not found")
            return False

        server = self.servers[server_name]
        try:
            # Check health endpoint
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{server.url}/health")
                if r.status_code == 200:
                    server.status = "connected"
                    # Discover tools
                    tools_r = await client.get(f"{server.url}/api/tools")
                    if tools_r.status_code == 200:
                        server.tools = tools_r.json().get("tools", [])
                    logger.info(f"Connected to MCP server: {server_name}")
                    return True
        except Exception as e:
            server.status = "error"
            logger.error(f"Failed to connect to {server_name}: {e}")
        return False

    async def send_command(self, server_name: str, command: str, args: Dict = None) -> Dict:
        """Envia comando a servidor MCP"""
        server = self.servers.get(server_name)
        if not server or server.status != "connected":
            return {"success": False, "error": f"Server '{server_name}' not connected"}

        try:
            import httpx
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{server.url}/api/chat",
                    json={"message": command, "args": args or {}},
                )
                return {
                    "success": r.status_code == 200,
                    "data": r.json() if r.status_code == 200 else None,
                    "server": server_name,
                }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def list_tools(self, server_name: str) -> List[Dict]:
        """Lista herramientas disponibles en un servidor MCP"""
        server = self.servers.get(server_name)
        if not server:
            return []

        if server.tools:
            return server.tools

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{server.url}/api/tools")
                if r.status_code == 200:
                    server.tools = r.json().get("tools", [])
                    return server.tools
        except Exception:
            pass
        return []

    async def execute_tool(self, server_name: str, tool_name: str, arguments: Dict) -> Dict:
        """Ejecuta herramienta en servidor MCP"""
        server = self.servers.get(server_name)
        if not server or server.status != "connected":
            return {"success": False, "error": f"Server '{server_name}' not connected"}

        try:
            import httpx
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{server.url}/api/tools/{tool_name}",
                    json={"arguments": arguments},
                )
                return {
                    "success": r.status_code == 200,
                    "data": r.json() if r.status_code == 200 else None,
                    "server": server_name,
                    "tool": tool_name,
                }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_status(self) -> Dict[str, str]:
        """Estado de todos los servidores MCP"""
        return {name: s.status for name, s in self.servers.items()}
