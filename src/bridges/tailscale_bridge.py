"""
Tailscale Bridge - Acceso global seguro via Tailscale VPN

Permite conectar NEXUS a maquinas en la tailnet desde cualquier lugar.
"""

import asyncio
import httpx
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class TailscaleNode:
    """Nodo en la tailnet"""
    name: str
    ip: str  # 100.x.x.x
    online: bool = False
    capabilities: List[str] = field(default_factory=list)
    last_seen: str = ""


class TailscaleBridge:
    """
    Bridge para conectar NEXUS con maquinas via Tailscale.
    Usa la API de Tailscale para descubrir nodos y SSH sobre tailnet IP.
    """

    def __init__(self, api_key: Optional[str] = None, tailnet: Optional[str] = None):
        self.api_key = api_key  # Se lee de env, no se hardcodea
        self.tailnet = tailnet
        self.nodes: Dict[str, TailscaleNode] = {}
        self.client = httpx.AsyncClient(timeout=10.0)

    async def discover_nodes(self) -> Dict[str, TailscaleNode]:
        """Descubre todos los nodos en la tailnet"""
        if not self.api_key or not self.tailnet:
            # Fallback: usar nodos conocidos
            self.nodes = self._load_known_nodes()
            return self.nodes

        try:
            r = await self.client.get(
                f"https://api.tailscale.com/api/v2/tailnet/{self.tailnet}/devices",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if r.status_code == 200:
                data = r.json()
                for device in data.get("devices", []):
                    name = device.get("name", "").split(".")[0]
                    ips = device.get("addresses", [])
                    self.nodes[name] = TailscaleNode(
                        name=name,
                        ip=ips[0] if ips else "unknown",
                        online=device.get("online", False),
                        last_seen=device.get("lastSeen", ""),
                    )
        except Exception as e:
            logger.error(f"Error discovering Tailscale nodes: {e}")
            self.nodes = self._load_known_nodes()

        return self.nodes

    def _load_known_nodes(self) -> Dict[str, TailscaleNode]:
        """Carga nodos conocidos sin API"""
        return {
            "windows": TailscaleNode(
                name="windows",
                ip="100.x.x.1",
                online=True,
                capabilities=["nexus", "opencode", "claude"],
            ),
            "remote_node": TailscaleNode(
                name="remote_node",
                ip="100.x.x.2",
                online=True,
                capabilities=["nexus", "openclaw", "antigravity", "gpu"],
            ),
        }

    async def ssh_to(self, node_name: str, command: str, timeout: int = 60) -> Dict[str, Any]:
        """Ejecuta comando via SSH sobre Tailscale IP"""
        node = self.nodes.get(node_name)
        if not node:
            return {"success": False, "error": f"Node '{node_name}' not found"}

        # Usar SSH sobre la IP de Tailscale (sin shell injection)
        import shlex
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
            node.ip, command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "success": proc.returncode == 0,
                "stdout": stdout.decode("utf-8", errors="replace").strip(),
                "stderr": stderr.decode("utf-8", errors="replace").strip(),
                "node": node_name,
            }
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Command timed out after {timeout}s"}

    async def http_to(self, node_name: str, port: int, path: str = "/") -> Dict[str, Any]:
        """Accede a servicio HTTP via Tailscale IP"""
        node = self.nodes.get(node_name)
        if not node:
            return {"success": False, "error": f"Node '{node_name}' not found"}

        try:
            r = await self.client.get(f"http://{node.ip}:{port}{path}", timeout=10.0)
            return {
                "success": r.status_code < 500,
                "status_code": r.status_code,
                "data": r.text[:5000] if r.status_code == 200 else None,
                "node": node_name,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def check_all_nodes(self) -> Dict[str, bool]:
        """Verifica estado de todos los nodos"""
        results = {}
        for name, node in self.nodes.items():
            try:
                r = await self.http_to(name, 9000, "/health")
                node.online = r.get("success", False)
                results[name] = node.online
            except Exception:
                node.online = False
                results[name] = False
        return results

    def list_nodes(self) -> Dict[str, Dict]:
        """Lista todos los nodos con info"""
        return {
            name: {
                "ip": n.ip,
                "online": n.online,
                "capabilities": n.capabilities,
                "last_seen": n.last_seen,
            }
            for name, n in self.nodes.items()
        }

    async def close(self):
        await self.client.aclose()
