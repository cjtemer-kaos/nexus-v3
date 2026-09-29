"""
API Chat Bridge - Comunicacion HTTP en tiempo real con otras IAs

Envia y recibe mensajes de otras IAs/servicios via HTTP.
Soporta streaming token por token y conversaciones multi-agente.
"""

import json
import logging
import os
from typing import Dict, List, AsyncGenerator
from dataclasses import dataclass, field

import httpx
from src.core import nexus_config

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class APIEndpoint:
    """Configuracion de un endpoint de API"""
    name: str
    url: str
    model: str = ""
    api_key: str = ""  # Se lee de env, no se hardcodea
    headers: Dict[str, str] = field(default_factory=dict)
    streaming: bool = True
    status: str = "unknown"


class APIChatBridge:
    """
    Bridge para comunicacion con otras IAs via HTTP.
    Soporta OpenAI-compatible API, streaming, y multi-agent conversations.
    """

    def __init__(self):
        self.endpoints: Dict[str, APIEndpoint] = {}
        self.client = httpx.AsyncClient(timeout=60.0)
        self._load_known_endpoints()

    def _load_known_endpoints(self):
        """Carga endpoints conocidos"""
        self.endpoints = {
            "ollama_local": APIEndpoint(
                name="ollama_local",
                url="http://127.0.0.1:11434",
                model="deepseek-r1:7b",
                streaming=True,
            ),
            "nexus_master": APIEndpoint(
                name="nexus_master",
                url=nexus_config.get_nexus_url(),
                streaming=True,
            ),
            "nexus_remote_node": APIEndpoint(
                name="nexus_remote_node",
                url=f"http://{os.getenv('SUPER_NEXUS_remote_node_IP', 'localhost')}:9000",
                streaming=True,
            ),
        }

    async def chat(self, endpoint_name: str, message: str, system: str = "", history: List[Dict] = None) -> Dict:
        """Envia mensaje y recibe respuesta completa"""
        ep = self.endpoints.get(endpoint_name)
        if not ep:
            return {"success": False, "error": f"Endpoint '{endpoint_name}' not found"}

        try:
            # Ollama-compatible API
            if "11434" in ep.url:
                payload = {
                    "model": ep.model,
                    "messages": self._build_messages(message, system, history),
                    "stream": False,
                }
                r = await self.client.post(f"{ep.url}/api/chat", json=payload)
            else:
                # Generic NEXUS API
                payload = {"message": message, "system": system, "history": history or []}
                r = await self.client.post(f"{ep.url}/api/chat", json=payload)

            if r.status_code == 200:
                ep.status = "online"
                return {"success": True, "data": r.json(), "endpoint": endpoint_name}
            else:
                ep.status = "error"
                return {"success": False, "error": f"HTTP {r.status_code}", "endpoint": endpoint_name}
        except Exception as e:
            ep.status = "offline"
            return {"success": False, "error": str(e), "endpoint": endpoint_name}

    async def stream_response(self, endpoint_name: str, message: str, system: str = "") -> AsyncGenerator[str, None]:
        """Recibe respuesta en streaming (token por token)"""
        ep = self.endpoints.get(endpoint_name)
        if not ep:
            return

        try:
            if "11434" in ep.url:
                payload = {
                    "model": ep.model,
                    "messages": self._build_messages(message, system),
                    "stream": True,
                }
                async with self.client.stream("POST", f"{ep.url}/api/chat", json=payload) as r:
                    async for line in r.aiter_lines():
                        if line:
                            data = json.loads(line)
                            if data.get("message", {}).get("content"):
                                yield data["message"]["content"]
            else:
                # Generic streaming
                payload = {"message": message, "system": system, "stream": True}
                async with self.client.stream("POST", f"{ep.url}/api/chat", json=payload) as r:
                    async for line in r.aiter_lines():
                        if line:
                            yield line
        except Exception as e:
            logger.error(f"Stream error: {e}")

    async def multi_agent_chat(self, endpoints: List[str], initial_message: str, max_rounds: int = 3) -> List[Dict]:
        """Orquesta conversacion entre multiples IAs"""
        conversation = []
        current_message = initial_message

        for round_num in range(max_rounds):
            for ep_name in endpoints:
                result = await self.chat(ep_name, current_message)
                if result["success"]:
                    response = result["data"].get("response", result["data"].get("message", ""))
                    conversation.append({
                        "round": round_num + 1,
                        "agent": ep_name,
                        "message": current_message[:100],
                        "response": response[:500],
                    })
                    current_message = response
                else:
                    conversation.append({
                        "round": round_num + 1,
                        "agent": ep_name,
                        "error": result["error"],
                    })

        return conversation

    def _build_messages(self, message: str, system: str = "", history: List[Dict] = None) -> List[Dict]:
        """Construye lista de mensajes para API"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": message})
        return messages

    async def check_all(self) -> Dict[str, str]:
        """Verifica estado de todos los endpoints"""
        results = {}
        for name, ep in self.endpoints.items():
            try:
                if "11434" in ep.url:
                    r = await self.client.get(f"{ep.url}/api/tags", timeout=5.0)
                else:
                    r = await self.client.get(f"{ep.url}/health", timeout=5.0)
                ep.status = "online" if r.status_code < 500 else "error"
                results[name] = ep.status
            except Exception:
                ep.status = "offline"
                results[name] = "offline"
        return results

    async def close(self):
        await self.client.aclose()
