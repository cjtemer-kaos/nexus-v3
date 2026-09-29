"""
ACP (Agent Communication Protocol) — comunicación interna formal entre agentes NEXUS.
Schema tipado, versionado, auditable. Reemplaza message_board adhoc.
"""
from __future__ import annotations
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class ACPMessageType(str, Enum):
    REQUEST = "request"
    RESPONSE = "response"
    NOTIFICATION = "notification"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


@dataclass
class ACPMessage:
    sender: str
    target: str
    msg_type: ACPMessageType
    payload: dict[str, Any]
    message_id: str = field(default_factory=lambda: f"acp-{uuid.uuid4().hex[:8]}")
    correlation_id: str = ""
    version: str = "1.0"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    ttl_s: int = 300

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id, "sender": self.sender, "target": self.target,
            "msg_type": self.msg_type.value, "payload": self.payload,
            "correlation_id": self.correlation_id, "version": self.version,
            "timestamp": self.timestamp, "ttl_s": self.ttl_s,
        }

    def is_expired(self) -> bool:
        created = datetime.fromisoformat(self.timestamp).timestamp()
        return (time.time() - created) > self.ttl_s

    def create_response(self, payload: dict) -> "ACPMessage":
        return ACPMessage(
            sender=self.target, target=self.sender,
            msg_type=ACPMessageType.RESPONSE, payload=payload,
            correlation_id=self.message_id,
        )


ACPHandler = Callable[[ACPMessage], Awaitable[ACPMessage | None]]


class ACPRouter:
    """Routes ACP messages to registered handlers per agent."""
    def __init__(self):
        self._handlers: dict[str, ACPHandler] = {}
        self._log: list[dict] = []

    def register(self, agent_name: str, handler: ACPHandler) -> None:
        self._handlers[agent_name] = handler

    def unregister(self, agent_name: str) -> None:
        self._handlers.pop(agent_name, None)

    @property
    def agents(self) -> list[str]:
        return list(self._handlers.keys())

    async def send(self, message: ACPMessage) -> ACPMessage | None:
        if message.is_expired():
            logger.warning(f"ACP: expired message {message.message_id}")
            return None
        handler = self._handlers.get(message.target)
        if not handler:
            logger.warning(f"ACP: no handler for {message.target}")
            return ACPMessage(
                sender="acp-router", target=message.sender,
                msg_type=ACPMessageType.ERROR,
                payload={"error": f"Unknown agent: {message.target}", "available": self.agents},
                correlation_id=message.message_id,
            )
        self._record(message)
        try:
            response = await handler(message)
            if response:
                self._record(response)
            return response
        except Exception as e:
            return ACPMessage(
                sender="acp-router", target=message.sender,
                msg_type=ACPMessageType.ERROR,
                payload={"error": str(e)}, correlation_id=message.message_id,
            )

    async def broadcast(self, message: ACPMessage) -> list[ACPMessage]:
        """Send to all registered agents except sender."""
        responses = []
        for name in self._handlers:
            if name == message.sender:
                continue
            msg = ACPMessage(sender=message.sender, target=name,
                           msg_type=message.msg_type, payload=message.payload)
            resp = await self.send(msg)
            if resp:
                responses.append(resp)
        return responses

    def _record(self, msg: ACPMessage) -> None:
        self._log.append({"id": msg.message_id, "sender": msg.sender,
                         "target": msg.target, "type": msg.msg_type.value,
                         "ts": msg.timestamp})
        if len(self._log) > 500:
            self._log = self._log[-500:]

    def status(self) -> dict:
        return {"agents": self.agents, "total_messages": len(self._log),
                "recent": self._log[-5:]}
