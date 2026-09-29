"""
SystemManager — Gestor de capacidades del Director.

Reemplaza los 18 init methods lineales por un sistema en capas con:
- Descubrimiento de modulos
- Verificacion de salud (health check)
- Reparacion automatica
- Degradacion gradual (sin LLM → capas 0-2)
- Reporte de estado
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class Layer(IntEnum):
    """Capas del sistema, de menor a mayor dependencia."""
    CORE = 0          # Identidad, config, logging — nunca falla
    BASE_TOOLS = 1     # shell, filesystem, system — no necesita IA
    MEMORY = 2         # neural, RAG, knowledge graph — local, offline
    LLM_ENGINE = 3     # providers, AgentRunner — necesita Ollama o API
    AGENTS = 4         # gemas, actores, loops — necesita Layer 3
    INTELLIGENCE = 5   # classify, goals, judge, training — necesita Layer 4


class CapabilityStatus:
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    REPAIRING = "repairing"
    DISABLED = "disabled"


@dataclass
class Capability:
    name: str
    layer: Layer
    description: str = ""
    dependencies: list[str] = field(default_factory=list)
    status: str = CapabilityStatus.UNKNOWN
    module_path: str = ""
    error: str = ""
    last_verified: str = ""
    init_fn: Optional[Callable[[], Awaitable[Any]]] = None
    health_fn: Optional[Callable[[], Awaitable[bool]]] = None
    repair_fn: Optional[Callable[[], Awaitable[bool]]] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_available(self) -> bool:
        return self.status in (CapabilityStatus.HEALTHY, CapabilityStatus.DEGRADED)


class SystemManager:
    """
    Gestor central de capacidades.

    Uso:
        mgr = SystemManager()
        mgr.register(Capability(name="shell", layer=Layer.BASE_TOOLS, ...))
        await mgr.verify()
        await mgr.repair()
        status = mgr.get_status()
    """

    def __init__(self):
        self._capabilities: dict[str, Capability] = {}
        self._initialized = False
        self._verify_lock = asyncio.Lock()

    # ── Registro ────────────────────────────────────────────────

    def register(self, cap: Capability) -> Capability:
        """Registra una capacidad."""
        self._capabilities[cap.name] = cap
        return cap

    def register_many(self, caps: list[Capability]) -> None:
        """Registra multiples capacidades."""
        for cap in caps:
            self.register(cap)

    def get(self, name: str) -> Optional[Capability]:
        return self._capabilities.get(name)

    @property
    def all(self) -> dict[str, Capability]:
        return dict(self._capabilities)

    # ── Verificacion ────────────────────────────────────────────

    async def verify(self) -> dict[str, str]:
        """
        Verifica todas las capacidades en orden de capa.
        Si una capa falla, las superiores se marcan como no disponibles.
        Retorna dict {name: status}.
        """
        async with self._verify_lock:
            results = {}
            layer_available = True

            for layer in sorted(set(c.layer for c in self._capabilities.values())):
                caps_in_layer = [c for c in self._capabilities.values() if c.layer == layer]
                layer_available = await self._verify_layer(caps_in_layer, layer_available, results)

            self._initialized = True
            return results

    async def _verify_layer(
        self,
        caps: list[Capability],
        parent_available: bool,
        results: dict[str, str],
    ) -> bool:
        """Verifica una capa completa. Retorna si la capa esta disponible.
        Una capa sin capacidades no bloquea a la siguiente."""
        if not caps:
            return True

        if not parent_available:
            intentar = all(
                self._check_deps_available(cap)
                for cap in caps
            )
            if not intentar:
                for cap in caps:
                    cap.status = CapabilityStatus.DISABLED
                    cap.error = f"Dependency layer {cap.layer - 1} failed"
                    results[cap.name] = cap.status
                return False

        any_healthy = False
        for cap in caps:
            await self._verify_one(cap)
            results[cap.name] = cap.status
            if cap.is_available:
                any_healthy = True

        return any_healthy

    def _check_deps_available(self, cap: Capability) -> bool:
        """Verifica si las dependencias explicitas de una capacidad estan disponibles."""
        for dep_name in cap.dependencies:
            dep = self._capabilities.get(dep_name)
            if dep and not dep.is_available:
                return False
        return True

    async def _verify_one(self, cap: Capability) -> None:
        """Verifica una capacidad individual."""
        cap.last_verified = datetime.now().isoformat()

        # Verificar dependencias
        for dep_name in cap.dependencies:
            dep = self._capabilities.get(dep_name)
            if dep and not dep.is_available:
                cap.status = CapabilityStatus.DISABLED
                cap.error = f"Dependency '{dep_name}' not available"
                return

        # Ejecutar init si existe (sync o async)
        if cap.init_fn and cap.status != CapabilityStatus.HEALTHY:
            try:
                result = cap.init_fn()
                if inspect.iscoroutine(result):
                    result = await result
                if result is False:
                    cap.status = CapabilityStatus.FAILED
                    cap.error = "init_fn returned False"
                    return
                if isinstance(result, Exception):
                    cap.status = CapabilityStatus.FAILED
                    cap.error = str(result)
                    return
            except Exception as e:
                cap.status = CapabilityStatus.FAILED
                cap.error = f"init error: {e}"
                return

        # Ejecutar health check
        if cap.health_fn:
            try:
                healthy = await self._run_fn(cap.health_fn)
                if healthy:
                    cap.status = CapabilityStatus.HEALTHY
                    cap.error = ""
                else:
                    cap.status = CapabilityStatus.FAILED
                    cap.error = "health check failed"
            except Exception as e:
                cap.status = CapabilityStatus.FAILED
                cap.error = f"health check error: {e}"
        else:
            cap.status = CapabilityStatus.HEALTHY

    # ── Reparacion ──────────────────────────────────────────────

    async def repair(self, name: Optional[str] = None) -> dict[str, bool]:
        """
        Intenta reparar capacidades fallidas.
        Si name es None, repara todas las fallidas en orden de capa.
        Retorna dict {name: repaired_ok}.
        """
        results = {}
        if name:
            cap = self._capabilities.get(name)
            if cap:
                results[name] = await self._repair_one(cap)
            return results

        for layer in sorted(set(c.layer for c in self._capabilities.values())):
            for cap in self._capabilities.values():
                if cap.layer == layer and cap.status == CapabilityStatus.FAILED:
                    results[cap.name] = await self._repair_one(cap)
        return results

    async def _repair_one(self, cap: Capability) -> bool:
        """Intenta reparar una capacidad."""
        old_status = cap.status
        cap.status = CapabilityStatus.REPAIRING

        if cap.repair_fn:
            try:
                repaired = await self._run_fn(cap.repair_fn)
                if repaired:
                    cap.status = CapabilityStatus.HEALTHY
                    cap.error = ""
                    logger.info(f"Repaired capability: {cap.name}")
                    return True
                else:
                    cap.status = old_status
                    return False
            except Exception as e:
                cap.error = f"repair error: {e}"
                cap.status = old_status
                return False

        # Sin repair_fn, intentar re-verify
        await self._verify_one(cap)
        return cap.is_available

    # ── Consulta ────────────────────────────────────────────────

    def is_available(self, name: str) -> bool:
        cap = self._capabilities.get(name)
        return cap.is_available if cap else False

    def is_layer_available(self, layer: Layer) -> bool:
        """Verifica si al menos una capacidad en la capa esta disponible."""
        return any(
            c.is_available
            for c in self._capabilities.values()
            if c.layer == layer
        )

    def get_available_layers(self) -> list[Layer]:
        """Retorna las capas que tienen al menos una capacidad disponible."""
        available = set()
        for cap in self._capabilities.values():
            if cap.is_available:
                available.add(cap.layer)
        return sorted(available)

    def get_status(self) -> dict:
        """Reporte completo de estado del sistema."""
        layers = {}
        for layer in Layer:
            caps = [c for c in self._capabilities.values() if c.layer == layer]
            if not caps:
                continue
            layers[layer.name] = {
                "total": len(caps),
                "healthy": sum(1 for c in caps if c.status == CapabilityStatus.HEALTHY),
                "degraded": sum(1 for c in caps if c.status == CapabilityStatus.DEGRADED),
                "failed": sum(1 for c in caps if c.status == CapabilityStatus.FAILED),
                "disabled": sum(1 for c in caps if c.status == CapabilityStatus.DISABLED),
                "unknown": sum(1 for c in caps if c.status == CapabilityStatus.UNKNOWN),
            }

        return {
            "initialized": self._initialized,
            "total": len(self._capabilities),
            "healthy": sum(1 for c in self._capabilities.values() if c.status == CapabilityStatus.HEALTHY),
            "degraded": sum(1 for c in self._capabilities.values() if c.status == CapabilityStatus.DEGRADED),
            "failed": sum(1 for c in self._capabilities.values() if c.status == CapabilityStatus.FAILED),
            "disabled": sum(1 for c in self._capabilities.values() if c.status == CapabilityStatus.DISABLED),
            "available_layers": [layer.name for layer in self.get_available_layers()],
            "layers": layers,
            "details": {
                name: {
                    "layer": cap.layer.name,
                    "status": cap.status,
                    "error": cap.error,
                    "dependencies": cap.dependencies,
                }
                for name, cap in self._capabilities.items()
            },
        }

    @staticmethod
    async def _run_fn(fn):
        """Ejecuta una funcion sync o async."""
        result = fn()
        if inspect.iscoroutine(result):
            return await result
        return result

    def log_summary(self) -> None:
        """Loggea resumen del sistema."""
        status = self.get_status()
        lines = [
            f"SystemManager: {status['healthy']}/{status['total']} healthy",
            f"  Layers available: {', '.join(status['available_layers'])}",
        ]
        failed = [n for n, c in self._capabilities.items() if c.status == CapabilityStatus.FAILED]
        if failed:
            lines.append(f"  Failed: {', '.join(failed)}")
        logger.info("\n".join(lines))
