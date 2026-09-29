"""
ConnectivityLayer - Capa unificada de conectividad para SuperNEXUS v2.0

Coordina acceso a todos los motores disponibles:
- NEXUS Master (local, port 9000)
- NEXUS PC2 (remoto, port 9000)
- OpenClaw Gateway (local, port 18789)
- REST Adapter (local, port 18790)
- SSH a maquinas remotas
- Tailscale tailnet
- MCP servers
- API Chat con otras IAs
"""

import asyncio
import logging
import os
import httpx
from src.core import nexus_config
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


class EngineType(Enum):
    LOCAL_HTTP = "local_http"
    REMOTE_HTTP = "remote_http"
    SSH = "ssh"
    TAILSCALE = "tailscale"
    MCP = "mcp"
    API_CHAT = "api_chat"


@dataclass
class EngineCapability:
    """Capacidades de un motor"""
    name: str
    type: EngineType
    url: str = ""
    host: str = ""
    port: int = 0
    capabilities: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    status: str = "unknown"
    last_check: str = ""


@dataclass
class EngineResult:
    """Resultado de ejecucion en un motor"""
    engine: str
    success: bool
    data: Any = None
    error: str = ""
    duration_ms: float = 0


class ConnectivityLayer:
    """
    Capa unificada de conectividad.
    Descubre, monitorea y ejecuta tareas en todos los motores disponibles.
    """

    def __init__(self):
        self.engines: Dict[str, EngineCapability] = {}
        self.client = httpx.AsyncClient(timeout=5.0)
        self._load_known_engines()
        self._cached_status: Optional[Dict[str, str]] = None
        self._last_check: float = 0
        self._cache_ttl: float = 30  # Cache status for 30 seconds

    def _load_known_engines(self):
        """Carga motores conocidos desde configuracion"""
        self.engines = {
            "nexus_master": EngineCapability(
                name="nexus_master",
                type=EngineType.LOCAL_HTTP,
                url=nexus_config.get_nexus_url(),
                host=nexus_config.get_host(),
                port=nexus_config.get_port(),
                capabilities=["skills", "chat", "memory", "director"],
                status="unknown",
            ),
            "nexus_pc2": EngineCapability(
                name="nexus_pc2",
                type=EngineType.REMOTE_HTTP,
                url=f"http://{os.getenv('SUPER_NEXUS_PC2_IP', 'localhost')}:9000",
                host=os.getenv("SUPER_NEXUS_PC2_IP", "localhost"),
                port=9000,
                capabilities=["skills", "chat", "memory", "gpu"],
                status="unknown" if not os.getenv("SUPER_NEXUS_PC2_IP") else "unknown",
            ),
            "openclaw_gateway": EngineCapability(
                name="openclaw_gateway",
                type=EngineType.LOCAL_HTTP,
                url="http://127.0.0.1:18789",
                host="127.0.0.1",
                port=18789,
                capabilities=["gateway", "ui", "api"],
                status="unknown",
            ),
            "rest_adapter": EngineCapability(
                name="rest_adapter",
                type=EngineType.LOCAL_HTTP,
                url="http://127.0.0.1:18790",
                host="127.0.0.1",
                port=18790,
                capabilities=["bridge", "openclaw_nexus"],
                status="unknown",
            ),
        }

    async def _check_single_engine(self, name: str, engine: EngineCapability) -> str:
        """Verifica un solo motor (reuso cliente compartido, evita auto-check)."""
        # Auto-check guard: si engine es localhost al mismo proceso, evitar deadlock.
        # El server haciendo HTTP a sí mismo mientras procesa el request lo bloquea.
        if name == "nexus_master" or "127.0.0.1" in engine.url or "localhost" in engine.url:
            engine.status = "self"
            engine.last_check = datetime.now().isoformat()
            return "self"

        try:
            if engine.type in (EngineType.LOCAL_HTTP, EngineType.REMOTE_HTTP):
                try:
                    r = await self.client.get(f"{engine.url}/health", timeout=2.0)
                except Exception:
                    r = await self.client.get(engine.url, timeout=2.0)
                engine.status = "online" if r.status_code < 500 else "error"
            elif engine.type == EngineType.SSH:
                engine.status = "unknown"
        except Exception:
            engine.status = "offline"

        engine.last_check = datetime.now().isoformat()
        return engine.status

    async def check_all_engines(self, force: bool = False) -> Dict[str, str]:
        """Verifica estado de todos los motores en paralelo con cache"""
        import time
        now = time.time()

        # Return cached status if fresh
        if not force and self._cached_status and (now - self._last_check) < self._cache_ttl:
            return self._cached_status

        # Check all engines in parallel
        tasks = [
            self._check_single_engine(name, engine)
            for name, engine in self.engines.items()
        ]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        results = {}
        for (name, engine), result in zip(self.engines.items(), results_list):
            if isinstance(result, Exception):
                engine.status = "offline"
                results[name] = "offline"
            else:
                results[name] = result

        self._cached_status = results
        self._last_check = now
        return results

    async def send_to_nexus(self, message: str, engine: str = "nexus_master") -> EngineResult:
        """Envia mensaje a un motor NEXUS"""
        start = datetime.now()
        try:
            eng = self.engines[engine]
            r = await self.client.post(
                f"{eng.url}/api/chat",
                json={"message": message},
                timeout=60.0,
            )
            duration = (datetime.now() - start).total_seconds() * 1000
            return EngineResult(
                engine=engine,
                success=r.status_code == 200,
                data=r.json() if r.status_code == 200 else None,
                error=f"HTTP {r.status_code}" if r.status_code != 200 else "",
                duration_ms=duration,
            )
        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return EngineResult(
                engine=engine,
                success=False,
                error=str(e),
                duration_ms=duration,
            )

    async def send_to_openclaw(self, message: str, via_adapter: bool = True) -> EngineResult:
        """Envia mensaje a OpenClaw (directo o via REST adapter)"""
        start = datetime.now()
        try:
            url = self.engines["rest_adapter"].url if via_adapter else self.engines["openclaw_gateway"].url
            r = await self.client.post(
                f"{url}/api/chat",
                json={"message": message},
                timeout=60.0,
            )
            duration = (datetime.now() - start).total_seconds() * 1000
            return EngineResult(
                engine="openclaw",
                success=r.status_code == 200,
                data=r.json() if r.status_code == 200 else None,
                error=f"HTTP {r.status_code}" if r.status_code != 200 else "",
                duration_ms=duration,
            )
        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return EngineResult(
                engine="openclaw",
                success=False,
                error=str(e),
                duration_ms=duration,
            )

    async def broadcast(self, message: str, engines: Optional[List[str]] = None) -> Dict[str, EngineResult]:
        """Envia mensaje a multiples motores en paralelo"""
        targets = engines or list(self.engines.keys())
        tasks = []
        for name in targets:
            if name.startswith("nexus"):
                tasks.append((name, self.send_to_nexus(message, name)))
            elif name.startswith("openclaw"):
                tasks.append((name, self.send_to_openclaw(message)))

        results = {}
        for name, coro in tasks:
            results[name] = await coro

        return results

    def get_online_engines(self) -> List[str]:
        """Retorna lista de motores online"""
        return [name for name, eng in self.engines.items() if eng.status == "online"]

    def get_capabilities(self) -> Dict[str, List[str]]:
        """Retorna capacidades de todos los motores"""
        return {name: eng.capabilities for name, eng in self.engines.items()}

    async def close(self):
        """Cierra conexiones"""
        await self.client.aclose()
