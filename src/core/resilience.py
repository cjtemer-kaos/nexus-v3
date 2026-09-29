"""
ResilienceLayer - Sistema de resiliencia y fallback automático para SuperNEXUS v2.0

Características:
- Health check continuo de todos los nodos
- Fallback automático: si PC2 no responde → usar local, si local falla → usar cloud
- Reinteligencia automática con backoff exponencial
- Circuit breaker pattern para evitar cascadas de fallos
"""

import asyncio
import logging
import threading
import time
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Circuit breaker para un servicio específico"""
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    half_open_max_calls: int = 3
    
    failures: int = 0
    last_failure_time: float = 0.0
    state: CircuitState = CircuitState.CLOSED
    half_open_calls: int = 0
    total_failures: int = 0
    total_successes: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    
    def can_execute(self) -> bool:
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            elif self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                    logger.info(f"Circuit breaker {self.name} → HALF_OPEN")
                    return True
                return False
            else:
                return self.half_open_calls < self.half_open_max_calls
    
    def record_success(self):
        with self._lock:
            self.total_successes += 1
            if self.state == CircuitState.HALF_OPEN:
                self.half_open_calls += 1
                if self.half_open_calls >= self.half_open_max_calls:
                    self.state = CircuitState.CLOSED
                    self.failures = 0
                    logger.info(f"Circuit breaker {self.name} → CLOSED (recovered)")
            elif self.state == CircuitState.OPEN:
                # Don't skip HALF_OPEN — just log that we're transitioning
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                logger.info(f"Circuit breaker {self.name} → HALF_OPEN (success in OPEN state)")
            else:
                self.failures = max(0, self.failures - 1)
    
    def record_failure(self):
        with self._lock:
            self.failures += 1
            self.total_failures += 1
            self.last_failure_time = time.time()
            
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit breaker {self.name} → OPEN (failed in HALF_OPEN)")
            elif self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit breaker {self.name} → OPEN (threshold reached)")


@dataclass
class HealthStatus:
    """Estado de salud de un nodo"""
    name: str
    status: str
    latency_ms: float = 0.0
    last_check: float = 0.0
    consecutive_failures: int = 0
    uptime_percentage: float = 100.0
    total_checks: int = 0
    failed_checks: int = 0


class ResilienceLayer:
    """
    Sistema de resiliencia y fallback automático.
    
    Uso:
        resilience = ResilienceLayer()
        result = await resilience.execute_with_fallback(
            task=my_task,
            engines=["nexus_pc2", "nexus_master", "openclaw_gateway"]
        )
    """
    
    def __init__(self):
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}
        self.health_history: Dict[str, HealthStatus] = {}
        self._health_check_interval = 30
        self._health_check_task = None
        self._running = False
        self._fallback_chain: Dict[str, List[str]] = {
            "nexus_pc2": ["nexus_master", "openclaw_gateway"],
            "nexus_master": ["nexus_pc2", "openclaw_gateway"],
            "openclaw_gateway": ["nexus_master", "nexus_pc2"],
        }
    
    def _get_circuit_breaker(self, engine: str) -> CircuitBreaker:
        if engine not in self.circuit_breakers:
            self.circuit_breakers[engine] = CircuitBreaker(name=engine)
        return self.circuit_breakers[engine]
    
    def _get_health_status(self, engine: str) -> HealthStatus:
        if engine not in self.health_history:
            self.health_history[engine] = HealthStatus(name=engine, status="unknown")
        return self.health_history[engine]
    
    async def execute_with_retry(
        self,
        func: Callable,
        *args,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        **kwargs
    ) -> Any:
        """Ejecuta función con reintentos y backoff exponencial"""
        last_exception = None
        
        for attempt in range(max_retries + 1):
            try:
                result = await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)
                if attempt > 0:
                    logger.info(f"Retry succeeded on attempt {attempt + 1}")
                return result
            except Exception as e:
                last_exception = e
                if attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"All {max_retries + 1} attempts failed")
        
        raise last_exception
    
    async def execute_with_circuit_breaker(
        self,
        engine: str,
        func: Callable,
        *args,
        **kwargs
    ) -> Any:
        """Ejecuta función con circuit breaker"""
        cb = self._get_circuit_breaker(engine)
        
        if not cb.can_execute():
            raise ConnectionError(f"Circuit breaker OPEN for {engine}. Service unavailable.")
        
        try:
            result = await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)
            cb.record_success()
            
            health = self._get_health_status(engine)
            health.consecutive_failures = 0
            return result
        except Exception as e:
            cb.record_failure()
            
            health = self._get_health_status(engine)
            health.consecutive_failures += 1
            health.failed_checks += 1
            
            raise e
    
    async def execute_with_fallback(
        self,
        task: str,
        engines: Optional[List[str]] = None,
        execute_func: Optional[Callable] = None,
        depth: int = 0,
        visited: Optional[set] = None,
        **kwargs
    ) -> Dict[str, Any]:
        if visited is None:
            visited = set()
        """
        Ejecuta tarea con fallback automático entre motores.
        
        Si el motor principal falla, intenta con el siguiente en la cadena.
        """
        if engines is None:
            engines = ["nexus_master"]
        
        errors = {}
        
        for engine in engines:
            cb = self._get_circuit_breaker(engine)
            
            if not cb.can_execute():
                logger.warning(f"Skipping {engine} (circuit breaker OPEN)")
                errors[engine] = "Circuit breaker OPEN"
                continue
            
            try:
                if execute_func:
                    result = await self.execute_with_circuit_breaker(
                        engine,
                        execute_func,
                        engine=engine,
                        task=task,
                        **kwargs
                    )
                else:
                    result = {"engine": engine, "data": f"Executed on {engine}"}
                
                health = self._get_health_status(engine)
                health.status = "online"
                health.total_checks += 1
                
                return {
                    "success": True,
                    "engine": engine,
                    "result": result,
                    "fallbacks_tried": list(errors.keys()),
                }
            except Exception as e:
                logger.error(f"Engine {engine} failed: {e}")
                errors[engine] = str(e)
                
                health = self._get_health_status(engine)
                health.status = "offline"
                health.total_checks += 1
                health.failed_checks += 1
                
                if engine in self._fallback_chain and depth < 5 and engine not in (visited or set()):
                    fallback_engines = self._fallback_chain[engine]
                    logger.info(f"Trying fallback engines: {fallback_engines}")
                    return await self.execute_with_fallback(
                        task=task,
                        engines=fallback_engines,
                        execute_func=execute_func,
                        depth=depth + 1,
                        visited=visited | {engine},
                        **kwargs
                    )
        
        return {
            "success": False,
            "engine": None,
            "result": None,
            "errors": errors,
            "fallbacks_tried": list(errors.keys()),
        }
    
    async def check_health(self, engine: str, check_func: Callable) -> HealthStatus:
        """Verifica salud de un nodo"""
        health = self._get_health_status(engine)
        start_time = time.time()
        
        try:
            await check_func()
            latency = (time.time() - start_time) * 1000
            
            health.status = "online"
            health.latency_ms = latency
            health.consecutive_failures = 0
            health.total_checks += 1
            health.last_check = time.time()
            
            cb = self._get_circuit_breaker(engine)
            cb.record_success()
            
        except Exception as e:
            latency = (time.time() - start_time) * 1000
            
            health.status = "offline"
            health.latency_ms = latency
            health.consecutive_failures += 1
            health.total_checks += 1
            health.failed_checks += 1
            health.last_check = time.time()
            
            cb = self._get_circuit_breaker(engine)
            cb.record_failure()
            
            logger.warning(f"Health check failed for {engine}: {e}")
        
        health.uptime_percentage = (
            ((health.total_checks - health.failed_checks) / health.total_checks) * 100
            if health.total_checks > 0 else 100.0
        )
        
        return health
    
    async def start_health_monitor(self, check_funcs: Dict[str, Callable]):
        """Inicia monitor de salud continuo"""
        if self._running:
            return
        
        self._running = True
        
        async def monitor_loop():
            while self._running:
                for engine, check_func in check_funcs.items():
                    try:
                        await self.check_health(engine, check_func)
                    except Exception as e:
                        logger.error(f"Health monitor error for {engine}: {e}")
                
                await asyncio.sleep(self._health_check_interval)
        
        self._health_check_task = asyncio.create_task(monitor_loop())
        logger.info("Health monitor started")
    
    def stop_health_monitor(self):
        """Detiene monitor de salud"""
        self._running = False
        if self._health_check_task:
            self._health_check_task.cancel()
        logger.info("Health monitor stopped")
    
    def get_status(self) -> Dict[str, Any]:
        """Obtiene estado completo de resiliencia"""
        return {
            "circuit_breakers": {
                name: {
                    "state": cb.state.value,
                    "failures": cb.failures,
                    "total_failures": cb.total_failures,
                    "total_successes": cb.total_successes,
                }
                for name, cb in self.circuit_breakers.items()
            },
            "health": {
                name: {
                    "status": h.status,
                    "latency_ms": h.latency_ms,
                    "uptime_percentage": h.uptime_percentage,
                    "consecutive_failures": h.consecutive_failures,
                }
                for name, h in self.health_history.items()
            },
        }
