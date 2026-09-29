"""
Remote Node Bridge - Puente genérico para nodo remoto en SuperNEXUS v2.0

Este módulo proporciona una interfaz genérica para conectar con un nodo remoto.
El usuario debe configurar las variables de entorno en su .env:
  SUPER_NEXUS_REMOTE_NODE_IP=tu_ip_aqui
  SUPER_NEXUS_REMOTE_NODE_USER=tu_usuario
  SUPER_NEXUS_REMOTE_NODE_PASSWORD=tu_contraseña

Adaptado para SuperNEXUS v2.0 - Versión distro limpia
"""

import logging
import os
from typing import Dict, Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


class RemoteNodeBridge:
    """Puente genérico para nodo remoto"""

    def __init__(self, host: str = None, port: int = 9000, user: str = None, password: str = None):
        self.host = host or os.environ.get("SUPER_NEXUS_REMOTE_NODE_IP", "")
        self.port = port or int(os.environ.get("SUPER_NEXUS_REMOTE_NODE_PORT", "9000"))
        self.user = user or os.environ.get("SUPER_NEXUS_REMOTE_NODE_USER", "")
        self.password_env = "SUPER_NEXUS_REMOTE_NODE_PASSWORD"
        self.base_url = f"http://{self.host}:{self.port}" if self.host else None
        self.session: Optional[aiohttp.ClientSession] = None
        self.connected = False

    def is_configured(self) -> bool:
        """Verifica si el nodo remoto está configurado"""
        return bool(self.host and self.user)

    async def connect(self) -> Dict[str, Any]:
        """Conecta al nodo remoto"""
        if not self.is_configured():
            logger.info("Remote node not configured. Set SUPER_NEXUS_REMOTE_NODE_IP in .env")
            return {"success": False, "error": "Remote node not configured"}

        try:
            self.session = aiohttp.ClientSession()
            async with self.session.get(f"{self.base_url}/api/status", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    self.connected = True
                    logger.info(f"Connected to remote node at {self.base_url}")
                    return {"success": True, "status": resp.status}
                return {"success": False, "error": f"Status {resp.status}"}
        except Exception as e:
            logger.warning(f"Remote node not available: {e}")
            return {"success": False, "error": str(e)}

    async def execute_remote(self, command: str, timeout: int = 60) -> Dict[str, Any]:
        """Ejecuta comando en nodo remoto via API"""
        if not self.connected or not self.session:
            return {"success": False, "error": "Not connected to remote node"}

        try:
            async with self.session.post(
                f"{self.base_url}/api/execute",
                json={"command": command},
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                data = await resp.json()
                return {"success": resp.status == 200, **data}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def run_ollama_model(self, model: str, prompt: str) -> Dict[str, Any]:
        """Ejecuta modelo de Ollama en nodo remoto"""
        if not self.connected or not self.session:
            return {"success": False, "error": "Not connected"}

        try:
            async with self.session.post(
                f"{self.base_url}/api/ollama/generate",
                json={"model": model, "prompt": prompt},
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                data = await resp.json()
                return {"success": resp.status == 200, **data}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_system_info(self) -> Dict[str, Any]:
        """Obtiene información del sistema remoto"""
        if not self.connected or not self.session:
            return {"success": False, "error": "Not connected"}

        try:
            async with self.session.get(
                f"{self.base_url}/api/system/info",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return {"success": resp.status == 200, **data}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_status(self) -> Dict[str, Any]:
        """Estado del nodo remoto"""
        return {
            "configured": self.is_configured(),
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "url": self.base_url,
        }

    async def close(self):
        """Cierra la conexión"""
        if self.session:
            await self.session.close()
            self.session = None
            self.connected = False
