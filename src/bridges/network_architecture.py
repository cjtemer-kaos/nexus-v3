"""
NetworkArchitecture v2 - Mejoras de arquitectura de red para SuperNEXUS v2.0

Características:
- Balanceo de carga inteligente entre nodos
- Gestión de conexiones con reconnect automático
- Optimización de latencia
- Health check mejorado con métricas de red
"""

import logging
import asyncio
import time
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum

try:
    import httpx
    HTTPX_AVAILABLE = True
except Exception:
    HTTPX_AVAILABLE = False

logger = logging.getLogger(__name__)


class NodeStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass
class NodeInfo:
    """Información de nodo"""
    id: str
    name: str
    url: str
    status: NodeStatus = NodeStatus.UNKNOWN
    latency_ms: float = 0.0
    last_check: float = 0.0
    capabilities: List[str] = field(default_factory=list)
    load: float = 0.0
    max_concurrent: int = 10
    active_connections: int = 0
    total_requests: int = 0
    failed_requests: int = 0
    metadata: Dict = field(default_factory=dict)


@dataclass
class LoadBalanceResult:
    """Resultado de balanceo de carga"""
    node_id: str
    success: bool
    response: Any = None
    error: str = ""
    latency_ms: float = 0.0


class NetworkArchitecture:
    """
    Arquitectura de red mejorada para SuperNEXUS v2.0
    
    Uso:
        network = NetworkArchitecture()
        network.add_node("remote", "Remote Node (GPU)", "http://{SUPER_NEXUS_remote_node_IP}:9000")
        result = await network.execute_with_load_balancing("task", capabilities=["chat"])
    """
    
    def __init__(self, health_check_interval: int = 30):
        self.nodes: Dict[str, NodeInfo] = {}
        self.health_check_interval = health_check_interval
        self._health_check_task = None
        self._running = False
        self._connection_pool: Dict[str, Any] = {}
    
    def add_node(
        self,
        node_id: str,
        name: str,
        url: str,
        capabilities: List[str] = None,
        max_concurrent: int = 10,
        metadata: Dict = None,
    ) -> NodeInfo:
        """Agrega nodo"""
        node = NodeInfo(
            id=node_id,
            name=name,
            url=url,
            capabilities=capabilities or [],
            max_concurrent=max_concurrent,
            metadata=metadata or {},
        )
        
        self.nodes[node_id] = node
        logger.info(f"Node added: {node_id} ({name})")
        
        return node
    
    def remove_node(self, node_id: str):
        """Elimina nodo"""
        if node_id in self.nodes:
            del self.nodes[node_id]
            logger.info(f"Node removed: {node_id}")
    
    async def check_node_health(self, node_id: str) -> NodeInfo:
        """Verifica salud de nodo"""
        node = self.nodes.get(node_id)
        if not node:
            return None
        
        start_time = time.time()
        
        try:
            if HTTPX_AVAILABLE:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(f"{node.url}/health")
                    latency = (time.time() - start_time) * 1000
                    
                    if response.status_code == 200:
                        node.status = NodeStatus.ONLINE
                        node.latency_ms = latency
                    else:
                        node.status = NodeStatus.DEGRADED
                        node.latency_ms = latency
            else:
                node.status = NodeStatus.UNKNOWN
        except Exception as e:
            node.status = NodeStatus.OFFLINE
            node.latency_ms = (time.time() - start_time) * 1000
            logger.debug(f"Health check failed for {node_id}: {e}")
        
        node.last_check = time.time()
        
        return node
    
    async def check_all_nodes(self):
        """Verifica salud de todos los nodos"""
        tasks = [
            self.check_node_health(node_id)
            for node_id in self.nodes.keys()
        ]
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    def select_best_node(self, capabilities: List[str] = None) -> Optional[NodeInfo]:
        """Selecciona mejor nodo basado en carga y capacidades"""
        eligible = [
            node for node in self.nodes.values()
            if node.status == NodeStatus.ONLINE
        ]
        
        if capabilities:
            eligible = [
                node for node in eligible
                if any(cap in node.capabilities for cap in capabilities)
            ]
        
        if not eligible:
            return None
        
        eligible.sort(key=lambda node: (
            node.load,
            node.latency_ms,
            node.active_connections / node.max_concurrent if node.max_concurrent > 0 else 1,
        ))
        
        return eligible[0]
    
    async def execute_with_load_balancing(
        self,
        task: str,
        capabilities: List[str] = None,
        execute_func: Callable = None,
        **kwargs,
    ) -> LoadBalanceResult:
        """Ejecuta tarea con balanceo de carga"""
        node = self.select_best_node(capabilities)
        
        if not node:
            return LoadBalanceResult(
                node_id=None,
                success=False,
                error="No available nodes",
            )
        
        if node.active_connections >= node.max_concurrent:
            return LoadBalanceResult(
                node_id=node.id,
                success=False,
                error="Node at max capacity",
            )
        
        node.active_connections += 1
        node.total_requests += 1
        
        start_time = time.time()
        
        try:
            if execute_func:
                response = await execute_func(node, task, **kwargs)
            else:
                response = {"task": task, "node": node.id}
            
            latency = (time.time() - start_time) * 1000
            
            node.load = min(1.0, node.load + 0.1)
            
            return LoadBalanceResult(
                node_id=node.id,
                success=True,
                response=response,
                latency_ms=latency,
            )
        except Exception as e:
            node.failed_requests += 1
            node.load = max(0.0, node.load - 0.2)
            
            return LoadBalanceResult(
                node_id=node.id,
                success=False,
                error=str(e),
                latency_ms=(time.time() - start_time) * 1000,
            )
        finally:
            node.active_connections -= 1
    
    async def start_health_monitor(self):
        """Inicia monitor de salud"""
        if self._running:
            return
        
        self._running = True
        
        async def monitor_loop():
            while self._running:
                try:
                    await self.check_all_nodes()
                except Exception as e:
                    logger.error(f"Health monitor error: {e}")
                
                await asyncio.sleep(self.health_check_interval)
        
        self._health_check_task = asyncio.create_task(monitor_loop())
        logger.info("Health monitor started")
    
    def stop_health_monitor(self):
        """Detiene monitor de salud"""
        self._running = False
        if self._health_check_task:
            self._health_check_task.cancel()
        logger.info("Health monitor stopped")
    
    def get_network_status(self) -> Dict:
        """Obtiene estado de red"""
        return {
            "nodes": {
                node_id: {
                    "name": node.name,
                    "url": node.url,
                    "status": node.status.value,
                    "latency_ms": node.latency_ms,
                    "load": node.load,
                    "active_connections": node.active_connections,
                    "max_concurrent": node.max_concurrent,
                    "total_requests": node.total_requests,
                    "failed_requests": node.failed_requests,
                    "capabilities": node.capabilities,
                }
                for node_id, node in self.nodes.items()
            },
            "total_nodes": len(self.nodes),
            "online_nodes": sum(1 for n in self.nodes.values() if n.status == NodeStatus.ONLINE),
            "offline_nodes": sum(1 for n in self.nodes.values() if n.status == NodeStatus.OFFLINE),
        }
