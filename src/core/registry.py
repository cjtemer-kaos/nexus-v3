"""ServiceRegistry — isolates service failures so one bad service can't kill NEXUS.

This is the heart of the v3 architecture. Before, every component was a hard
attribute on the director:
    self.recursive_seed = RecursiveSeedAI()  # if this throws → boot dies

Now, each service is registered + inited in isolation. If `recursive_seed`
throws during init, the registry marks it failed, logs the error, and keeps
going. The rest of the system stays up.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.core.interfaces.service import BaseService

if TYPE_CHECKING:
    from src.core.nexus import NexusApp

logger = logging.getLogger(__name__)


class ServiceRegistry:
    """Holds services by name. Initializes them with isolated try/except.

    Stagger between inits (default 0.1s) prevents the Windows Proactor
    saturation that was killing NEXUS before.
    """

    def __init__(self):
        self._services: Dict[str, BaseService] = {}
        self._init_errors: Dict[str, str] = {}
        self._init_done: bool = False

    # ── Registration ─────────────────────────────────────────────────────

    def register(self, service: BaseService) -> "ServiceRegistry":
        """Register a service. No I/O — just records it in the table."""
        if not service.name:
            raise ValueError(
                f"Service {type(service).__name__} has empty `name` attribute"
            )
        if service.name in self._services:
            logger.warning(
                f"Service '{service.name}' already registered — overwriting"
            )
        self._services[service.name] = service
        return self

    # ── Access ───────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[BaseService]:
        """Get a service by name. Returns None if not registered OR init failed.

        This is the safe accessor — callers should check for None and
        gracefully degrade.
        """
        if name in self._init_errors:
            return None
        return self._services.get(name)

    def require(self, name: str) -> BaseService:
        """Get a service, raising if missing or failed.

        Use this only when the caller cannot function without the service.
        """
        if name in self._init_errors:
            raise RuntimeError(
                f"Service '{name}' failed to initialize: {self._init_errors[name]}"
            )
        svc = self._services.get(name)
        if svc is None:
            raise KeyError(
                f"Service '{name}' not registered. "
                f"Available: {sorted(self._services.keys())}"
            )
        return svc

    def list_services(self) -> List[str]:
        """All registered service names (regardless of init status)."""
        return list(self._services.keys())

    def list_healthy(self) -> List[str]:
        """Names of services that initialized successfully."""
        return [n for n in self._services if n not in self._init_errors]

    def list_failed(self) -> Dict[str, str]:
        """Map of failed service names to error messages."""
        return dict(self._init_errors)

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def init_all(
        self,
        app: "NexusApp",
        stagger_s: float = 0.1,
        timeout_s: float = 30.0,
    ) -> Dict[str, str]:
        """Initialize every registered service with isolated try/except.

        Args:
            app: the NexusApp passed to each service's init(app).
            stagger_s: pause between inits to avoid event loop saturation.
            timeout_s: per-service init timeout.

        Returns:
            {service_name: "ok" | "error: <msg>"} for the caller to log.
        """
        results: Dict[str, str] = {}
        for name, svc in self._services.items():
            try:
                await asyncio.wait_for(svc.init(app), timeout=timeout_s)
                results[name] = "ok"
                logger.info(f"[registry] '{name}' initialized")
            except asyncio.TimeoutError:
                msg = f"timeout after {timeout_s}s"
                self._init_errors[name] = msg
                results[name] = f"error: {msg}"
                logger.error(f"[registry] '{name}' init timeout ({timeout_s}s)")
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:200]}"
                self._init_errors[name] = msg
                results[name] = f"error: {msg}"
                logger.error(f"[registry] '{name}' init failed: {msg}")
            if stagger_s > 0:
                await asyncio.sleep(stagger_s)
        self._init_done = True
        return results

    async def start_all(self, stagger_s: float = 0.1, timeout_s: float = 10.0) -> None:
        """Run start() on each healthy service.

        Failed-init services are skipped. Each start() call is isolated and
        bounded by timeout_s.
        """
        for name, svc in self._services.items():
            if name in self._init_errors:
                continue
            try:
                await asyncio.wait_for(svc.start(), timeout=timeout_s)
            except asyncio.TimeoutError:
                logger.warning(f"[registry] '{name}' start() timed out ({timeout_s}s)")
            except Exception as e:
                logger.warning(f"[registry] '{name}' start() failed: {e}")
            if stagger_s > 0:
                await asyncio.sleep(stagger_s)

    async def stop_all(self, timeout_s: float = 5.0) -> None:
        """Stop every service. Errors are logged but never raised."""
        for name, svc in self._services.items():
            try:
                await asyncio.wait_for(svc.stop(), timeout=timeout_s)
            except Exception as e:
                logger.warning(f"[registry] '{name}' stop() failed: {e}")

    # ── Status ───────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, dict]:
        """Aggregate status — asks each service for its own snapshot.

        Failed services report their error; running services call get_status();
        get_status() exceptions are caught so /api/status never 500s.
        """
        out: Dict[str, dict] = {}
        for name, svc in self._services.items():
            if name in self._init_errors:
                out[name] = {
                    "name": name,
                    "status": "init_failed",
                    "error": self._init_errors[name],
                }
                continue
            try:
                out[name] = svc.get_status()
            except Exception as e:
                out[name] = {
                    "name": name,
                    "status": "status_error",
                    "error": f"{type(e).__name__}: {str(e)[:120]}",
                }
        return out

    def summary(self) -> Dict[str, Any]:
        """Quick numeric summary."""
        return {
            "total": len(self._services),
            "healthy": len(self.list_healthy()),
            "failed": len(self._init_errors),
            "init_done": self._init_done,
        }
