"""NexusApp — minimal App container, opencode-style.

NexusApp does NOT contain logic. It just composes services that the rest of
the system needs.

Usage:
    app = NexusApp(project="default")
    app.register(MemoryService())
    app.register(ToolService())
    app.register(GemaService())
    await app.boot()

    # later, anywhere:
    mem = app.get("memory")  # returns None if init failed
    tools = app.require("tools")  # raises if missing

Why this exists:
    Before, the director had 30+ services as direct attributes
    (self.recursive_seed, self.peer_chat, ...). One bad init killed boot
    and one missing attribute crashed /api/status with 500.
    Now: services live in a registry; failures are isolated; the director
    asks the app for what it needs, when it needs it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.core.interfaces.service import BaseService
from src.core.registry import ServiceRegistry

logger = logging.getLogger(__name__)


class NexusApp:
    """Container of NEXUS services. The "App" of the system."""

    def __init__(self, project: str = "default"):
        self.project = project
        self.registry = ServiceRegistry()
        self._started: bool = False
        self._init_results: Dict[str, str] = {}

    # ── Registration (fluent) ────────────────────────────────────────────

    def register(self, service: BaseService) -> "NexusApp":
        """Register a service. Chainable: app.register(A()).register(B())."""
        self.registry.register(service)
        return self

    # ── Access ───────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[BaseService]:
        """Get a service, or None if not registered/failed."""
        return self.registry.get(name)

    def require(self, name: str) -> BaseService:
        """Get a service, raising if missing/failed."""
        return self.registry.require(name)

    def has(self, name: str) -> bool:
        """Check whether a service is registered AND healthy."""
        return self.registry.get(name) is not None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def boot(self, stagger_s: float = 0.1) -> Dict[str, str]:
        """Initialize and start every registered service.

        Returns:
            init results dict for logging/diagnostics.
        """
        n = len(self.registry.list_services())
        logger.info(f"NexusApp booting (project={self.project!r}, services={n})")
        self._init_results = await self.registry.init_all(app=self, stagger_s=stagger_s)
        await self.registry.start_all(stagger_s=stagger_s)
        self._started = True
        summary = self.registry.summary()
        logger.info(
            f"NexusApp booted: {summary['healthy']}/{summary['total']} healthy"
        )
        if summary["failed"]:
            logger.warning(f"NexusApp failed services: {self.registry.list_failed()}")
        return self._init_results

    async def shutdown(self) -> None:
        """Stop all services and mark as not running."""
        if not self._started:
            return
        logger.info("NexusApp shutting down")
        await self.registry.stop_all()
        self._started = False

    # ── Status ───────────────────────────────────────────────────────────

    def is_started(self) -> bool:
        return self._started

    def get_status(self) -> Dict[str, Any]:
        """Full snapshot — fast, never raises."""
        return {
            "project": self.project,
            "started": self._started,
            "summary": self.registry.summary(),
            "services": self.registry.get_status(),
            "init_results": self._init_results,
        }

    def __repr__(self) -> str:
        return (
            f"<NexusApp project={self.project!r} "
            f"services={len(self.registry.list_services())} "
            f"started={self._started}>"
        )
