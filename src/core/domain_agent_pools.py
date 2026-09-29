"""
Domain-Based Agent Pools - Sistema de pools de agentes por dominio

Inspirado en domain-based agent pools:
- Queen Coordinator (Agent 1): Central orchestrator
- Security Domain (Agents 2-4): Parallel execution
- Core Domain (Agents 5-9): Parallel execution
- Integration Domain (Agents 10-12): Parallel execution
- Quality (Agent 13): Sequential
- Performance (Agent 14): Sequential
- Deployment (Agent 15): Sequential

Problema actual:
- 22 gemas en tabla plana sin organización por dominio
- Routing semántico básico sin consideración de dominio
- No hay pools paralelos por dominio

Solución:
- Organizar gemas en 5 dominios: Orquestación, Desarrollo, Seguridad, Infraestructura, Creativo
- Cada dominio tiene un pool de gemas que pueden ejecutarse en paralelo
- Task routing por dominio + scoring multi-factor
- Soporte para ejecución paralela dentro del mismo dominio

Arquitectura:
- DomainRouter: routing de tareas a dominios
- AgentPool: pool de gemas por dominio con scoring
- TaskDecomposer: descomposición de tareas complejas
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class Domain(Enum):
    """Dominios de agentes"""
    ORCHESTRATION = "orchestration"    # Director, sage, biblioteca, ayuda
    DEVELOPMENT = "development"        # code, opencode, debugger, tester
    SECURITY = "security"              # security, optimizer
    INFRASTRUCTURE = "infrastructure"  # devops, producer, architect
    CREATIVE = "creative"              # creative, design, music, vision
    RESEARCH = "research"              # scholar, analyst, trainer


@dataclass
class DomainConfig:
    """Configuración de un dominio"""
    name: str
    domain: Domain
    gemas: List[str]
    parallel_capable: bool = True
    max_parallel: int = 3
    description: str = ""


@dataclass
class TaskScore:
    """Score de una tarea para un agente"""
    gema_name: str
    capability_score: float = 0.0    # Match de capacidades (30%)
    load_score: float = 1.0          # Carga actual (20%) - menor es mejor
    performance_score: float = 0.5   # Histórico de performance (25%)
    health_score: float = 1.0        # Salud del agente (15%)
    availability_score: float = 1.0  # Disponibilidad (10%)
    
    @property
    def total_score(self) -> float:
        return (
            self.capability_score * 0.30 +
            self.load_score * 0.20 +
            self.performance_score * 0.25 +
            self.health_score * 0.15 +
            self.availability_score * 0.10
        )


@dataclass
class DomainTask:
    """Tarea asignada a un dominio"""
    task: str
    domain: Domain
    assigned_gemas: List[str]
    can_parallelize: bool = False
    priority: int = 3
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class AgentPool:
    """Pool de agentes para un dominio específico"""
    
    def __init__(self, config: DomainConfig):
        self.config = config
        self.gema_loads: Dict[str, int] = {g: 0 for g in config.gemas}
        self.gema_performance: Dict[str, float] = {g: 0.5 for g in config.gemas}
        self.gema_health: Dict[str, bool] = {g: True for g in config.gemas}
        self.execution_history: List[Dict] = []
    
    def score_gemas(self, task: str, capabilities: Dict[str, List[str]]) -> List[TaskScore]:
        """Score multi-factor para seleccionar mejores gemas"""
        scores = []
        task_lower = task.lower()
        
        for gema in self.config.gemas:
            caps = capabilities.get(gema, [])
            
            # Capability score: cuántas keywords de la tarea matchean capacidades
            cap_matches = sum(1 for cap in caps if cap.lower() in task_lower)
            capability_score = min(cap_matches / max(len(caps), 1), 1.0)
            
            # Load score: inversamente proporcional a carga actual
            load = self.gema_loads.get(gema, 0)
            load_score = 1.0 - min(load / 10.0, 1.0)
            
            # Performance score: histórico de éxito
            performance_score = self.gema_performance.get(gema, 0.5)
            
            # Health score: 1.0 si healthy, 0.0 si no
            health_score = 1.0 if self.gema_health.get(gema, False) else 0.0
            
            # Availability score: 1.0 si no está ejecutando
            availability_score = 1.0 if load == 0 else 0.5
            
            score = TaskScore(
                gema_name=gema,
                capability_score=capability_score,
                load_score=load_score,
                performance_score=performance_score,
                health_score=health_score,
                availability_score=availability_score,
            )
            scores.append(score)
        
        # Ordenar por score total descendente
        scores.sort(key=lambda s: s.total_score, reverse=True)
        return scores
    
    def select_gemas(self, task: str, capabilities: Dict[str, List[str]], top_k: int = 2) -> List[str]:
        """Seleccionar top_k gemas para una tarea"""
        scores = self.score_gemas(task, capabilities)
        return [s.gema_name for s in scores[:top_k] if s.total_score > 0.1]
    
    def record_execution(self, gema: str, success: bool, duration_ms: float):
        """Registrar ejecución para actualizar métricas"""
        self.gema_loads[gema] = max(0, self.gema_loads.get(gema, 0) - 1)
        
        # Update performance (exponential moving average)
        current_perf = self.gema_performance.get(gema, 0.5)
        new_perf = 1.0 if success else 0.0
        self.gema_performance[gema] = current_perf * 0.9 + new_perf * 0.1
        
        self.execution_history.append({
            "gema": gema,
            "success": success,
            "duration_ms": duration_ms,
            "timestamp": datetime.now().isoformat(),
        })
        
        # Keep only last 100 executions
        if len(self.execution_history) > 100:
            self.execution_history = self.execution_history[-100:]
    
    def get_stats(self) -> Dict:
        return {
            "domain": self.config.domain.value,
            "gemas": self.config.gemas,
            "parallel_capable": self.config.parallel_capable,
            "current_loads": dict(self.gema_loads),
            "performance": {k: round(v, 3) for k, v in self.gema_performance.items()},
            "health": dict(self.gema_health),
            "total_executions": len(self.execution_history),
        }


class DomainAgentPools:
    """
    Gestiona pools de agentes organizados por dominio.
    
    Inspirado en domain-based agent pools:
    - Queen-led hierarchical mesh
    - Domain partitioning (orchestration, development, security, etc.)
    - Multi-factor agent scoring
    - Parallel execution within domains
    
    Uso:
        pools = DomainAgentPools()
        pools.build_from_gemas(director.gemas)
        
        # Routing de tarea
        task = "fix python bug in authentication"
        domain, gemas = pools.route_task(task)
        
        # Ejecución paralela
        if domain.parallel_capable:
            results = await execute_parallel(gemas, task)
    """
    
    # Configuración de dominios
    DOMAIN_CONFIGS = [
        DomainConfig(
            name="Orquestación",
            domain=Domain.ORCHESTRATION,
            gemas=["director", "sage", "biblioteca", "ayuda"],
            parallel_capable=False,
            description="Coordinación central y memoria",
        ),
        DomainConfig(
            name="Desarrollo",
            domain=Domain.DEVELOPMENT,
            gemas=["code", "opencode", "debugger", "tester", "optimizer"],
            parallel_capable=True,
            max_parallel=3,
            description="Desarrollo, debugging y testing",
        ),
        DomainConfig(
            name="Seguridad",
            domain=Domain.SECURITY,
            gemas=["security", "prompter"],
            parallel_capable=True,
            max_parallel=2,
            description="Seguridad y optimización",
        ),
        DomainConfig(
            name="Infraestructura",
            domain=Domain.INFRASTRUCTURE,
            gemas=["devops", "producer", "architect"],
            parallel_capable=True,
            max_parallel=2,
            description="DevOps, automatización y arquitectura",
        ),
        DomainConfig(
            name="Creativo",
            domain=Domain.CREATIVE,
            gemas=["creative", "design", "music", "vision"],
            parallel_capable=True,
            max_parallel=2,
            description="Contenido creativo y multimedia",
        ),
        DomainConfig(
            name="Investigación",
            domain=Domain.RESEARCH,
            gemas=["scholar", "analyst", "trainer"],
            parallel_capable=True,
            max_parallel=2,
            description="Investigación, análisis y entrenamiento",
        ),
    ]
    
    def __init__(self):
        self.pools: Dict[Domain, AgentPool] = {}
        self.gema_to_domain: Dict[str, Domain] = {}
        self.gema_capabilities: Dict[str, List[str]] = {}
    
    def build_from_gemas(self, gemas: Dict) -> None:
        """Construir pools desde las gemas del Director"""
        for config in self.DOMAIN_CONFIGS:
            pool = AgentPool(config)
            self.pools[config.domain] = pool
            
            for gema_name in config.gemas:
                self.gema_to_domain[gema_name] = config.domain
                if gema_name in gemas:
                    self.gema_capabilities[gema_name] = gemas[gema_name].tags
        
        logger.info(f"Domain agent pools built: {len(self.pools)} domains, "
                   f"{len(self.gema_to_domain)} gemas")
    
    def route_task(self, task: str, selected_gemas: List[str]) -> DomainTask:
        """
        Routear tarea al dominio apropiado.
        
        Determina qué dominio y qué gemas dentro del dominio
        son mejores para la tarea.
        """
        # Determinar dominio basado en gemas seleccionadas
        domain_counts = {}
        for gema in selected_gemas:
            domain = self.gema_to_domain.get(gema)
            if domain:
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
        
        # Dominio con más gemas seleccionadas
        if domain_counts:
            primary_domain = max(domain_counts, key=domain_counts.get)
        else:
            primary_domain = Domain.ORCHESTRATION  # Default
        
        # Seleccionar gemas óptimas dentro del dominio
        pool = self.pools[primary_domain]
        optimal_gemas = pool.select_gemas(task, self.gema_capabilities, top_k=2)
        
        # Determinar si puede paralelizar
        can_parallelize = (
            self.pools[primary_domain].config.parallel_capable and
            len(optimal_gemas) > 1
        )
        
        return DomainTask(
            task=task,
            domain=primary_domain,
            assigned_gemas=optimal_gemas,
            can_parallelize=can_parallelize,
        )
    
    def get_pool(self, domain: Domain) -> Optional[AgentPool]:
        """Obtener pool de un dominio"""
        return self.pools.get(domain)
    
    def get_domain_for_gema(self, gema: str) -> Optional[Domain]:
        """Obtener dominio de una gema"""
        return self.gema_to_domain.get(gema)
    
    def record_execution(self, gema: str, success: bool, duration_ms: float):
        """Registrar ejecución en el pool correspondiente"""
        domain = self.gema_to_domain.get(gema)
        if domain and domain in self.pools:
            self.pools[domain].record_execution(gema, success, duration_ms)
    
    def get_stats(self) -> Dict:
        """Obtener estadísticas de todos los pools"""
        return {
            domain.value: pool.get_stats()
            for domain, pool in self.pools.items()
        }
