"""
Graceful Degradation System - Patrón de degradación elegante

    Inspirado en graceful degradation pattern:
- AgentDB -> in-memory fallback
- Coordinator -> simple mode fallback
- HNSW -> brute-force fallback
- Every subsystem has a fallback path

Problema:
- Si un subsistema falla, todo el Director puede colapsar
- No hay fallback paths para componentes críticos

Solución:
- Cada componente registra un fallback
- Si el componente principal falla, se usa el fallback
- El sistema sigue funcionando con capacidades reducidas
- Logging de degradaciones para monitoreo

Arquitectura:
- GracefulDegradationManager gestiona todos los fallbacks
- Cada componente tiene primary_fn y fallback_fn
- Health checks automáticos detectan fallos
- Recovery automático cuando el componente principal se recupera
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ComponentStatus:
    """Estado de un componente con graceful degradation"""
    name: str
    healthy: bool = True
    degraded: bool = False
    using_fallback: bool = False
    last_check: str = field(default_factory=lambda: datetime.now().isoformat())
    error_count: int = 0
    fallback_count: int = 0
    last_error: Optional[str] = None
    recovery_time: Optional[float] = None


@dataclass
class FallbackConfig:
    """Configuración de fallback para un componente"""
    name: str
    primary_fn: Callable
    fallback_fn: Callable
    health_check_fn: Optional[Callable] = None
    max_errors_before_fallback: int = 3
    recovery_check_interval: int = 60  # seconds


class GracefulDegradationManager:
    """
    Gestiona graceful degradation para todos los componentes del Director.
    
Inspirado en graceful degradation pattern:
    - AgentDB -> in-memory fallback
    - Coordinator -> simple mode fallback
    - HNSW -> brute-force fallback
    
    Uso:
        mgr = GracefulDegradationManager()
        mgr.register_component(
            name="memory",
            primary_fn=memory_consolidator.consolidate,
            fallback_fn=lambda x: {"status": "degraded", "data": x},
            health_check_fn=memory_consolidator.is_healthy,
        )
        
        # Ejecutar con fallback automático
        result = await mgr.execute("memory", data)
    """
    
    def __init__(self):
        self.components: Dict[str, FallbackConfig] = {}
        self.statuses: Dict[str, ComponentStatus] = {}
        self._error_counts: Dict[str, int] = {}
        self._recovery_tasks: Dict[str, asyncio.Task] = {}
    
    def register_component(
        self,
        name: str,
        primary_fn: Callable,
        fallback_fn: Callable,
        health_check_fn: Optional[Callable] = None,
        max_errors_before_fallback: int = 3,
    ) -> None:
        """Registrar un componente con su fallback"""
        self.components[name] = FallbackConfig(
            name=name,
            primary_fn=primary_fn,
            fallback_fn=fallback_fn,
            health_check_fn=health_check_fn,
            max_errors_before_fallback=max_errors_before_fallback,
        )
        self.statuses[name] = ComponentStatus(name=name)
        self._error_counts[name] = 0
        logger.info(f"Registered component: {name} (max_errors: {max_errors_before_fallback})")
    
    async def execute(self, name: str, *args, **kwargs) -> Any:
        """
        Ejecutar componente con graceful degradation.
        
        Si el componente principal falla, usa el fallback automáticamente.
        """
        config = self.components.get(name)
        if not config:
            raise ValueError(f"Component not registered: {name}")
        
        status = self.statuses[name]
        status.last_check = datetime.now().isoformat()
        
        # Si ya está usando fallback, intentar recovery
        if status.using_fallback:
            return await self._try_recovery_execute(config, status, *args, **kwargs)
        
        # Intentar ejecutar componente principal
        try:
            result = config.primary_fn(*args, **kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            
            # Reset error count on success
            self._error_counts[name] = 0
            status.healthy = True
            status.degraded = False
            return result
        except Exception as e:
            self._error_counts[name] += 1
            status.error_count += 1
            status.last_error = f"{type(e).__name__}: {str(e)}"
            
            logger.warning(f"Component {name} failed (error {self._error_counts[name]}/{config.max_errors_before_fallback}): {e}")
            
            # Verificar si debemos cambiar a fallback
            if self._error_counts[name] >= config.max_errors_before_fallback:
                logger.warning(f"Component {name} switching to fallback mode")
                status.using_fallback = True
                status.degraded = True
                status.fallback_count += 1
            
            # Ejecutar fallback
            return await self._execute_fallback(config, status, *args, **kwargs)
    
    async def _execute_fallback(self, config: FallbackConfig, status: ComponentStatus, *args, **kwargs) -> Any:
        """Ejecutar función de fallback"""
        try:
            result = config.fallback_fn(*args, **kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            
            logger.info(f"Component {config.name} executed fallback successfully")
            return result
        except Exception as e:
            logger.error(f"Component {config.name} fallback also failed: {e}")
            status.last_error = f"Fallback failed: {type(e).__name__}: {str(e)}"
            raise
    
    async def _try_recovery_execute(self, config: FallbackConfig, status: ComponentStatus, *args, **kwargs) -> Any:
        """Intentar recuperar componente principal"""
        # Verificar health si hay health_check_fn
        if config.health_check_fn:
            try:
                is_healthy = config.health_check_fn()
                if asyncio.iscoroutine(is_healthy):
                    is_healthy = await is_healthy
                
                if is_healthy:
                    logger.info(f"Component {config.name} recovered, switching back to primary")
                    status.using_fallback = False
                    status.degraded = False
                    status.healthy = True
                    self._error_counts[config.name] = 0
                    status.recovery_time = time.time()
                    
                    # Ejecutar con primary
                    result = config.primary_fn(*args, **kwargs)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return result
            except Exception:
                pass  # Still unhealthy, continue with fallback
        
        # Ejecutar fallback
        return await self._execute_fallback(config, status, *args, **kwargs)
    
    def get_status(self, name: Optional[str] = None) -> Dict:
        """Obtener estado de componentes"""
        if name:
            status = self.statuses.get(name)
            if not status:
                return {"error": f"Component not found: {name}"}
            return {
                "name": status.name,
                "healthy": status.healthy,
                "degraded": status.degraded,
                "using_fallback": status.using_fallback,
                "error_count": status.error_count,
                "fallback_count": status.fallback_count,
                "last_error": status.last_error,
                "last_check": status.last_check,
            }
        
        return {
            name: {
                "healthy": s.healthy,
                "degraded": s.degraded,
                "using_fallback": s.using_fallback,
                "error_count": s.error_count,
                "fallback_count": s.fallback_count,
                "last_error": s.last_error,
            }
            for name, s in self.statuses.items()
        }
    
    def get_degraded_components(self) -> List[str]:
        """Obtener lista de componentes degradados"""
        return [
            name for name, status in self.statuses.items()
            if status.degraded or status.using_fallback
        ]
