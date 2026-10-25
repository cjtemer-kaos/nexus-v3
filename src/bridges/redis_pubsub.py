"""
Redis PubSub Bridge para Hermes ↔ SuperNEXUS ↔ OpenCode

Proporciona comunicación real-time entre agentes via Redis.
Canales:
  - nexus:agents    — Heartbeat/estado de agentes
  - nexus:tasks     — Tareas asignadas/completadas
  - nexus:memory    — Actualizaciones de memoria
  - nexus:chat      — Mensajes entre agentes
  - nexus:system    — Eventos del sistema

Uso desde agentes (via MCP tools):
  redis_publish(channel, message)
  redis_subscribe(channel)
  redis_read_messages(channel, limit)
"""
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Redis connection
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("redis package not installed")

# Canales estándar
CHANNELS = {
    "agents": "nexus:agents",
    "tasks": "nexus:tasks",
    "memory": "nexus:memory",
    "chat": "nexus:chat",
    "system": "nexus:system",
}

MSG_PREFIX = "nexus:msg:"


class RedisPubSub:
    """PubSub wrapper para comunicación real-time entre agentes."""

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0):
        if not REDIS_AVAILABLE:
            raise RuntimeError("redis package not installed: pip install redis")
        self._r = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        self._pubsub = self._r.pubsub()
        self._subscriptions: Dict[str, List[Callable]] = {}
        self._running = False

    def ping(self) -> bool:
        try:
            return self._r.ping()
        except Exception:
            return False

    def publish(self, channel: str, message: Dict[str, Any], source: str = "opencode") -> str:
        msg_id = f"msg_{int(time.time() * 1000)}"
        full_channel = CHANNELS.get(channel, f"nexus:{channel}")
        payload = {
            "id": msg_id,
            "source": source,
            "channel": channel,
            "timestamp": datetime.now().isoformat(),
            "data": message,
        }
        history_key = f"{MSG_PREFIX}{channel}"
        self._r.lpush(history_key, json.dumps(payload))
        self._r.ltrim(history_key, 0, 99)
        self._r.publish(full_channel, json.dumps(payload))
        logger.debug(f"Published to {full_channel}: {msg_id}")
        return msg_id

    def subscribe(self, channel: str, callback: Callable[[Dict], None]) -> None:
        full_channel = CHANNELS.get(channel, f"nexus:{channel}")
        if channel not in self._subscriptions:
            self._subscriptions[channel] = []
        self._subscriptions[channel].append(callback)
        self._pubsub.subscribe(full_channel)
        logger.info(f"Subscribed to {full_channel}")

    def unsubscribe(self, channel: str) -> None:
        full_channel = CHANNELS.get(channel, f"nexus:{channel}")
        self._pubsub.unsubscribe(full_channel)
        self._subscriptions.pop(channel, None)
        logger.info(f"Unsubscribed from {full_channel}")

    def get_messages(self, channel: str, limit: int = 10) -> List[Dict]:
        history_key = f"{MSG_PREFIX}{channel}"
        raw = self._r.lrange(history_key, 0, limit - 1)
        return [json.loads(m) for m in raw]

    def get_agents(self) -> List[Dict]:
        agents_key = "nexus:agents:heartbeat"
        raw = self._r.hgetall(agents_key)
        result = []
        for agent_id, data in raw.items():
            try:
                info = json.loads(data)
                if time.time() - info.get("timestamp", 0) < 60:
                    result.append(info)
            except Exception:
                pass
        return result

    def heartbeat(self, agent_id: str, status: str = "active") -> None:
        agents_key = "nexus:agents:heartbeat"
        data = {
            "agent_id": agent_id,
            "status": status,
            "timestamp": time.time(),
        }
        self._r.hset(agents_key, agent_id, json.dumps(data))
        self._r.expire(agents_key, 120)

    def close(self) -> None:
        try:
            self._pubsub.close()
            self._r.close()
        except Exception:
            pass


_instance: Optional[RedisPubSub] = None


def get_redis() -> RedisPubSub:
    global _instance
    if _instance is None:
        host = os.environ.get("REDIS_HOST", "localhost")
        port = int(os.environ.get("REDIS_PORT", "6379"))
        _instance = RedisPubSub(host=host, port=port)
    return _instance


# MCP Tool wrappers
def redis_publish(channel: str, message: str, source: str = "opencode") -> str:
    try:
        r = get_redis()
        data = json.loads(message) if isinstance(message, str) else message
        msg_id = r.publish(channel, data, source)
        return json.dumps({"success": True, "message_id": msg_id})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def redis_subscribe(channel: str) -> str:
    try:
        get_redis()  # solo verifica que el backend responda; el poll va por get_messages
        return json.dumps({"success": True, "channel": channel, "note": "Use get_messages to poll"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def redis_get_messages(channel: str, limit: int = 10) -> str:
    try:
        r = get_redis()
        messages = r.get_messages(channel, limit)
        return json.dumps({"success": True, "messages": messages, "count": len(messages)})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def redis_list_agents() -> str:
    try:
        r = get_redis()
        agents = r.get_agents()
        return json.dumps({"success": True, "agents": agents, "count": len(agents)})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def redis_heartbeat(agent_id: str, status: str = "active") -> str:
    try:
        r = get_redis()
        r.heartbeat(agent_id, status)
        return json.dumps({"success": True, "agent_id": agent_id})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


if __name__ == "__main__":
    r = RedisPubSub()
    print(f"Redis ping: {r.ping()}")
    msg_id = r.publish("chat", {"text": "Hello from OpenCode!"}, source="opencode")
    print(f"Published: {msg_id}")
    msgs = r.get_messages("chat", 5)
    print(f"Messages: {len(msgs)}")
    r.close()
