import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("nexus-mcp-pool")


@dataclass
class ConnectionStats:
    acquired: int = 0
    released: int = 0
    errors: int = 0
    reconnects: int = 0
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    last_health_check: float = 0.0
    health_check_failures: int = 0


@dataclass
class PooledConnection:
    server_name: str
    client: Any
    refcount: int = 1
    stats: ConnectionStats = field(default_factory=ConnectionStats)
    healthy: bool = True


class MCPConnectionPool:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, bridge=None, max_connections: int = 10, health_check_interval: float = 30.0):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._initialized = True
        self._bridge = bridge
        self._max_connections = max_connections
        self._health_check_interval = health_check_interval
        self._pool: Dict[str, PooledConnection] = {}
        self._lock = threading.Lock()
        self._health_task: Optional[asyncio.Task] = None

    async def acquire(self, server_name: str, auto_start: bool = True) -> Any:
        with self._lock:
            existing = self._pool.get(server_name)
            if existing:
                existing.refcount += 1
                existing.stats.acquired += 1
                existing.stats.last_used = time.time()
                return existing.client

            if len(self._pool) >= self._max_connections:
                self._evict_oldest()

        if not self._bridge:
            return None

        client = await self._bridge.start_server(server_name)
        if not client and auto_start:
            started = await self._bridge.start_server(server_name)
            if not started:
                return None
            client = self._bridge._servers.get(server_name)

        if client:
            conn = PooledConnection(server_name=server_name, client=client)
            with self._lock:
                self._pool[server_name] = conn
            conn.stats.acquired += 1

        return client

    def release(self, server_name: str):
        with self._lock:
            conn = self._pool.get(server_name)
            if not conn:
                return
            conn.refcount -= 1
            conn.stats.released += 1
            if conn.refcount <= 0:
                self._pool.pop(server_name, None)

    async def health_check(self, server_name: str) -> bool:
        conn = self._pool.get(server_name)
        if not conn:
            return False

        if time.time() - conn.stats.last_health_check < self._health_check_interval:
            return conn.healthy

        conn.stats.last_health_check = time.time()
        try:
            if self._bridge:
                svr = self._bridge._servers.get(server_name)
                if svr and svr.process:
                    poll = svr.process.poll()
                    if poll is not None:
                        raise RuntimeError(f"Process exited with code {poll}")
                conn.healthy = True
                conn.stats.health_check_failures = 0
                return True
        except Exception:
            conn.stats.health_check_failures += 1
            conn.healthy = False

        return conn.healthy

    async def reconnect(self, server_name: str) -> bool:
        with self._lock:
            conn = self._pool.get(server_name)
            if conn and conn.client:
                if self._bridge:
                    await self._bridge.stop_server(server_name)
            self._pool.pop(server_name, None)
            conn.stats.reconnects += 1 if conn else 0

        client = await self.acquire(server_name, auto_start=True)
        return client is not None

    async def health_check_all(self):
        for server_name in list(self._pool.keys()):
            ok = await self.health_check(server_name)
            if not ok:
                logger.warning(f"MCP server {server_name} unhealthy, reconnecting...")
                await self.reconnect(server_name)

    async def cleanup_all(self):
        with self._lock:
            names = list(self._pool.keys())
            self._pool.clear()
        for name in names:
            if self._bridge:
                await self._bridge.stop_server(name)

    def get_stats(self) -> Dict:
        with self._lock:
            return {
                "active_connections": len(self._pool),
                "max_connections": self._max_connections,
                "connections": {
                    name: {
                        "refcount": conn.refcount,
                        "healthy": conn.healthy,
                        "acquired": conn.stats.acquired,
                        "released": conn.stats.released,
                        "errors": conn.stats.errors,
                        "reconnects": conn.stats.reconnects,
                        "uptime_seconds": round(time.time() - conn.stats.created_at, 1),
                    }
                    for name, conn in self._pool.items()
                },
            }

    def _evict_oldest(self):
        if not self._pool:
            return
        oldest = min(self._pool.items(), key=lambda x: x[1].stats.last_used)
        self._pool.pop(oldest[0], None)
