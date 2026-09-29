"""
Protocol Router — Unifica MCP, A2A, ACP bajo un solo entry point.
Decide qué protocolo usar basado en el target y tipo de mensaje.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class Protocol(str, Enum):
    MCP = "mcp"
    A2A = "a2a"
    ACP = "acp"
    HTTP = "http"
    CLI = "cli"


@dataclass
class ServiceEntry:
    name: str
    protocol: Protocol
    endpoint: str
    capabilities: list[str] = field(default_factory=list)
    healthy: bool = True
    last_seen: float = 0.0


class DiscoveryService:
    """Registry of available services and their protocols."""
    def __init__(self):
        self._services: dict[str, ServiceEntry] = {}

    def register(self, entry: ServiceEntry) -> None:
        self._services[entry.name] = entry

    def unregister(self, name: str) -> None:
        self._services.pop(name, None)

    def discover(self, capability: str) -> list[ServiceEntry]:
        """Find services that have a given capability."""
        return [s for s in self._services.values()
                if capability in s.capabilities and s.healthy]

    def get(self, name: str) -> ServiceEntry | None:
        return self._services.get(name)

    @property
    def services(self) -> list[ServiceEntry]:
        return list(self._services.values())

    def status(self) -> dict:
        return {
            "total": len(self._services),
            "healthy": sum(1 for s in self._services.values() if s.healthy),
            "services": {s.name: {"protocol": s.protocol.value, "healthy": s.healthy,
                                  "capabilities": s.capabilities}
                        for s in self._services.values()},
        }


class ProtocolRouter:
    """Routes messages to the correct protocol handler."""
    def __init__(self):
        self.discovery = DiscoveryService()
        self._protocol_handlers: dict[Protocol, Any] = {}

    def register_protocol(self, protocol: Protocol, handler: Any) -> None:
        self._protocol_handlers[protocol] = handler

    def route(self, target: str) -> tuple[Protocol, Any] | None:
        """Find the protocol and handler for a target."""
        service = self.discovery.get(target)
        if not service:
            return None
        handler = self._protocol_handlers.get(service.protocol)
        if not handler:
            return None
        return (service.protocol, handler)

    def best_for_capability(self, capability: str) -> ServiceEntry | None:
        """Find the best service for a capability."""
        candidates = self.discovery.discover(capability)
        if not candidates:
            return None
        priority = {Protocol.ACP: 0, Protocol.MCP: 1, Protocol.A2A: 2, Protocol.HTTP: 3, Protocol.CLI: 4}
        candidates.sort(key=lambda s: priority.get(s.protocol, 99))
        return candidates[0]

    def status(self) -> dict:
        return {
            "protocols": [p.value for p in self._protocol_handlers],
            "discovery": self.discovery.status(),
        }
