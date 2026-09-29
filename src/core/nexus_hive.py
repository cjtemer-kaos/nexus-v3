"""
NexusHive - Sistema de Comunicación en Tiempo Real via Redis Pub/Sub

Permite comunicación bidireccional entre nodos (PC1, PC2, etc.)
usando Redis como sistema nervioso central.

Canal principal: nexus_hive_commands
Canal de resultados: nexus_results_{cmd_id}
"""

import asyncio
import json
import logging
import os
import uuid
import time
from typing import Dict, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("nexus-hive")


class NodeStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    ERROR = "error"


@dataclass
class HiveNode:
    """Representa un nodo en la colmena Nexus"""
    id: str
    name: str
    host: str
    status: NodeStatus = NodeStatus.OFFLINE
    capabilities: list = field(default_factory=list)
    last_seen: float = 0
    metadata: dict = field(default_factory=dict)


class NexusHive:
    """
    Sistema de comunicación en tiempo real via Redis Pub/Sub.
    
    Arquitectura:
    - Canal de comandos: nexus_hive_commands
    - Canal de resultados: nexus_results_{cmd_id}
    - Canal de heartbeat: nexus_heartbeat
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.redis: Optional[Any] = None
        self.pubsub: Optional[Any] = None
        self.nodes: Dict[str, HiveNode] = {}
        self._running = False
        self._command_handlers: Dict[str, Callable] = {}
        self._pending_commands: Dict[str, asyncio.Future] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._listener_task: Optional[asyncio.Task] = None

        # Registrar nodos conocidos
        self._register_known_nodes()

    def _register_known_nodes(self):
        """Registra nodos conocidos en la red"""
        pc2_ip = os.getenv("SUPER_NEXUS_PC2_IP", "")
        self.nodes = {
            "local": HiveNode(
                id="local",
                name="Local Node",
                host="127.0.0.1",
                status=NodeStatus.ONLINE,
                capabilities=["nexus", "voice", "brain", "ui", "redis"],
            ),
        }
        if pc2_ip:
            self.nodes["remote"] = HiveNode(
                id="remote",
                name="Remote Node (GPU)",
                host=pc2_ip,
                status=NodeStatus.OFFLINE,
                capabilities=["gpu", "ollama", "openclaw", "antigravity", "hermes",
                               "aider", "goose", "pincer", "lethe", "mem0",
                               "agency-agents", "qwen-code", "ollama-direct",
                               "zen-free", "zen-go"],
            )

    async def connect(self) -> bool:
        """Conecta al servidor Redis"""
        if aioredis is None:
            logger.warning("redis.asyncio no disponible, usando modo offline")
            return False

        try:
            self.redis = await asyncio.wait_for(
                aioredis.Redis(
                    host=self.host,
                    port=self.port,
                    db=self.db,
                    password=self.password,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                ),
                timeout=3,
            )
            await asyncio.wait_for(self.redis.ping(), timeout=2)
            logger.info(f"Conectado a Redis en {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.warning(f"Redis no disponible, modo offline: {e}")
            self.redis = None
            return False

    async def start(self):
        """Inicia el sistema NexusHive"""
        if not await self.connect():
            logger.info("NexusHive en modo offline (Redis no disponible)")
            return

        if not self.redis:
            logger.warning("Redis connection lost after connect() succeeded")
            return

        self._running = True
        self.pubsub = self.redis.pubsub()
        await self.pubsub.subscribe("nexus_hive_commands")

        # Iniciar tareas en segundo plano
        self._listener_task = asyncio.create_task(self._listen_commands())
        self._heartbeat_task = asyncio.create_task(self._send_heartbeat())

        logger.info("NexusHive iniciado - escuchando comandos")

    async def stop(self):
        """Detiene el sistema NexusHive"""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._listener_task:
            self._listener_task.cancel()
        if self.pubsub and self.redis:
            try:
                await self.pubsub.unsubscribe()
                await self.pubsub.close()
            except Exception:
                pass
        if self.redis:
            try:
                await self.redis.close()
            except Exception:
                pass
        logger.info("NexusHive detenido")

    async def send_command(
        self,
        command: str,
        target_node: Optional[str] = None,
        timeout: int = 30,
        **kwargs,
    ) -> Dict:
        """
        Envía un comando a la colmena y espera respuesta.
        Usa Redis pub/sub si está disponible, fallback a HTTP directo.
        """
        # Si hay target node específico, intentar HTTP directo primero
        if target_node and target_node in self.nodes:
            node = self.nodes[target_node]
            if node.status == NodeStatus.OFFLINE:
                return await self._send_http_fallback(node, command, timeout, **kwargs)

        if not self.redis:
            # Intentar HTTP a todos los nodos conocidos
            return await self._broadcast_http(command, timeout, **kwargs)

        cmd_id = str(uuid.uuid4())[:8]
        payload = {
            "id": cmd_id,
            "command": command,
            "target": target_node,
            "source": "pc1",
            "timestamp": time.time(),
            "args": kwargs,
        }

        # Crear future para esperar respuesta
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending_commands[cmd_id] = future

        # Suscribirse al canal de resultados
        result_channel = f"nexus_results_{cmd_id}"
        result_pubsub = self.redis.pubsub()
        await result_pubsub.subscribe(result_channel)

        # Publicar comando
        await self.redis.publish("nexus_hive_commands", json.dumps(payload))
        logger.info(f"Comando enviado [{cmd_id}]: {command}")

        # Iniciar listener de resultados
        asyncio.create_task(self._listen_results(result_pubsub, result_channel, future))

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            return {"status": "timeout", "error": f"Timeout después de {timeout}s"}
        finally:
            self._pending_commands.pop(cmd_id, None)
            await result_pubsub.unsubscribe(result_channel)
            await result_pubsub.close()

    async def _listen_commands(self):
        """Escucha comandos entrantes del canal principal"""
        try:
            async for message in self.pubsub.listen():
                if message["type"] != "message":
                    continue
                
                try:
                    data = json.loads(message["data"])
                    cmd_id = data.get("id")
                    command = data.get("command")
                    target = data.get("target")

                    # Si hay target y no somos nosotros, ignorar
                    if target and target != "pc1":
                        continue

                    logger.info(f"Comando recibido [{cmd_id}]: {command}")

                    # Ejecutar handler si existe
                    if command in self._command_handlers:
                        handler = self._command_handlers[command]
                        result = await handler(**data.get("args", {}))
                    else:
                        result = {"status": "error", "error": f"Comando desconocido: {command}"}

                    # Publicar resultado
                    result_channel = f"nexus_results_{cmd_id}"
                    await self.redis.publish(result_channel, json.dumps(result))

                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.error(f"Error procesando comando: {e}")
        except asyncio.CancelledError:
            pass

    async def _listen_results(self, pubsub, channel, future):
        """Escucha resultados de un comando específico"""
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    result = json.loads(message["data"])
                    if not future.done():
                        future.set_result(result)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not future.done():
                future.set_exception(e)

    async def _send_heartbeat(self):
        """Envía heartbeat periódico"""
        while self._running:
            try:
                heartbeat = {
                    "type": "heartbeat",
                    "source": "pc1",
                    "timestamp": time.time(),
                    "nodes": {
                        node_id: {
                            "status": node.status.value,
                            "last_seen": node.last_seen,
                        }
                        for node_id, node in self.nodes.items()
                    },
                }
                await self.redis.publish("nexus_heartbeat", json.dumps(heartbeat))
            except Exception:
                pass
            await asyncio.sleep(5)

    def register_handler(self, command: str, handler: Callable):
        """Registra un handler para un comando"""
        self._command_handlers[command] = handler

    async def _send_http_fallback(self, node: HiveNode, command: str, timeout: int, **kwargs) -> Dict:
        """Envía comando via HTTP directo al nodo"""
        import httpx
        
        # Intentar 1: Nexus API con "message" (SuperNEXUS v2)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(
                    f"http://{node.host}:9000/api/chat",
                    json={"message": command, "gem": kwargs.get("gem", "auto")},
                )
                data = r.json()
                if r.status_code == 200 and "reply" in data:
                    node.status = NodeStatus.ONLINE
                    node.last_seen = time.time()
                    return {"status": "success", "node": node.id, "data": data}
        except Exception:
            pass

        # Intentar 2: Nexus API con "prompt" (Nexus legacy)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(
                    f"http://{node.host}:9000/api/chat",
                    json={"prompt": command, "gem": kwargs.get("gem", "sage")},
                )
                data = r.json()
                if r.status_code == 200 and "reply" in data:
                    node.status = NodeStatus.ONLINE
                    node.last_seen = time.time()
                    return {"status": "success", "node": node.id, "data": data}
        except Exception:
            pass

        # Intentar 3: Ollama directo
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(
                    f"http://{node.host}:11434/api/generate",
                    json={"model": "llama3.2:3b", "prompt": command, "stream": False},
                )
                data = r.json()
                if r.status_code == 200 and "response" in data:
                    node.status = NodeStatus.ONLINE
                    node.last_seen = time.time()
                    return {
                        "status": "success",
                        "node": node.id,
                        "data": {"reply": data["response"], "model": data.get("model", ""), "method": "ollama_direct"},
                    }
        except Exception:
            pass

        node.status = NodeStatus.OFFLINE
        return {"status": "error", "node": node.id, "error": "All connection methods failed"}

    async def _broadcast_http(self, command: str, timeout: int, **kwargs) -> Dict:
        """Broadcast HTTP a todos los nodos"""
        results = {}
        import httpx
        for node_id, node in self.nodes.items():
            if node_id == "pc1":
                continue
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.post(
                        f"http://{node.host}:9000/api/chat",
                        json={"message": command},
                    )
                    if r.status_code == 200:
                        node.status = NodeStatus.ONLINE
                        node.last_seen = time.time()
                        results[node_id] = {"status": "success", "data": r.json()}
                    else:
                        results[node_id] = {"status": "error", "http_status": r.status_code}
            except Exception:
                node.status = NodeStatus.OFFLINE
                results[node_id] = {"status": "offline"}
        return {"status": "broadcast", "results": results}

    def get_nodes(self) -> Dict[str, Dict]:
        """Obtiene estado de todos los nodos"""
        return {
            node_id: {
                "name": node.name,
                "host": node.host,
                "status": node.status.value,
                "capabilities": node.capabilities,
                "last_seen": node.last_seen,
            }
            for node_id, node in self.nodes.items()
        }

    def get_status(self) -> Dict:
        """Estado del sistema NexusHive"""
        return {
            "connected": self.redis is not None,
            "running": self._running,
            "nodes": self.get_nodes(),
            "pending_commands": len(self._pending_commands),
            "registered_handlers": list(self._command_handlers.keys()),
        }
