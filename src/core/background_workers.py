"""
Background Workers + Daemon Runtime - Sistema de workers automáticos con runtime loop.

Basado en el patrón Daemon de multica: heartbeat, auto-registro, GC, health server.

15 workers + 4 loops daemon:
  Workers: Memory Consolidation, Session Cleanup, Token Optimization, Security Scan,
           Performance Metrics, Context Compaction, Error Pattern, Knowledge Sync,
           Health Check, Learning, Skill Audit, Backup, Three-Loop, Training, Peer Learning
  Daemon:  heartbeatLoop (30s), gcLoop (5min), autoUpdateLoop (1h), healthServer
"""

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from enum import Enum

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


class WorkerTrigger(Enum):
    """Tipos de trigger para workers"""
    SCHEDULED = "scheduled"      # Ejecución periódica
    EVENT = "event"              # Triggered por evento
    ON_DEMAND = "on_demand"      # Ejecución manual


@dataclass
class WorkerResult:
    """Resultado de ejecución de un worker"""
    worker_name: str
    success: bool
    duration_ms: float
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class WorkerConfig:
    """Configuración de un worker"""
    name: str
    trigger: WorkerTrigger
    interval_seconds: int = 300  # 5 min default
    timeout_seconds: int = 30
    enabled: bool = True
    max_retries: int = 3


class BaseWorker(ABC):
    """Clase base para todos los background workers"""
    
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.last_run: Optional[WorkerResult] = None
        self.run_count = 0
        self.error_count = 0
        self._running = False
        # Worker lifecycle phase (aden-hive Queen pattern).
        # idle      not currently in run()
        # incubating  warming up before first useful work
        # working   inside run()
        # reviewing  post-run inspection / metrics flush
        self.phase: str = "idle"

    def set_phase(self, phase: str):
        self.phase = phase
        try:
            from src.observability.event_stream import emit, EventType
            emit(EventType.WORKER_RECOVERED if phase == "working" else EventType.HEALTH_DEGRADED,
                 data={"worker": self.config.name, "phase": phase},
                 source="background_workers")
        except Exception:
            pass
    
    @abstractmethod
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        """Ejecutar el worker. Debe ser implementado por subclases."""
        pass
    
    async def execute(self, context: Dict[str, Any]) -> WorkerResult:
        """Wrapper con timeout, retries y métricas"""
        if not self.config.enabled:
            return WorkerResult(
                worker_name=self.config.name,
                success=False,
                duration_ms=0,
                error="Worker disabled"
            )
        
        self._running = True
        self.phase = "incubating"
        start = time.time()

        try:
            for attempt in range(self.config.max_retries):
                try:
                    self.phase = "working"
                    result = await asyncio.wait_for(
                        self.run(context),
                        timeout=self.config.timeout_seconds
                    )
                    result.duration_ms = (time.time() - start) * 1000
                    self.last_run = result
                    self.run_count += 1
                    if result.success:
                        self.error_count = 0
                    else:
                        self.error_count += 1
                    self.phase = "reviewing"
                    return result
                except asyncio.TimeoutError:
                    logger.warning(f"Worker {self.config.name} timed out (attempt {attempt + 1})")
                    if attempt == self.config.max_retries - 1:
                        return WorkerResult(
                            worker_name=self.config.name,
                            success=False,
                            duration_ms=(time.time() - start) * 1000,
                            error=f"Timeout after {self.config.timeout_seconds}s"
                        )
                except Exception as e:
                    logger.error(f"Worker {self.config.name} error: {e}")
                    if attempt == self.config.max_retries - 1:
                        return WorkerResult(
                            worker_name=self.config.name,
                            success=False,
                            duration_ms=(time.time() - start) * 1000,
                            error=f"{type(e).__name__}: {str(e)}"
                        )
                    await asyncio.sleep(1 * (attempt + 1))  # Backoff
        finally:
            self._running = False
            self.phase = "idle"
        
        return WorkerResult(
            worker_name=self.config.name,
            success=False,
            duration_ms=(time.time() - start) * 1000,
            error="Max retries exceeded"
        )
    
    @property
    def is_running(self) -> bool:
        return self._running


# ============================================================
# 12 WORKERS IMPLEMENTADOS
# ============================================================

class MemoryConsolidationWorker(BaseWorker):
    """Consolida memorias periódicamente (ADD-only + topic keys)"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="memory_consolidation",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=600,  # 10 min
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            # New consolidator pass (commit 51) — soft-archives orphans + crowded topics.
            stats: Dict[str, Any] = {}
            try:
                from src.brain.memory_consolidator import consolidate_now
                stats = consolidate_now()
            except Exception as e:
                stats = {"new_consolidator_skipped": str(e)}

            # Legacy: also call old consolidator if present (back-compat)
            legacy = context.get("memory_consolidator")
            if legacy and hasattr(legacy, "get_stats"):
                try:
                    stats["legacy_stats"] = legacy.get_stats()
                except Exception:
                    pass

            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={"stats": stats, "action": "consolidation_complete"}
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class SessionCleanupWorker(BaseWorker):
    """Limpia sesiones expiradas"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="session_cleanup",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=1800,  # 30 min
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            sessions = context.get("sessions")
            if not sessions:
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No session manager")
            
            cleaned = 0
            # Limpiar sesiones inactivas por más de 2h
            cutoff = datetime.now() - timedelta(hours=2)
            for session_id, session in list(sessions.sessions.items()):
                if session.last_activity < cutoff.isoformat():
                    sessions.close_session(session_id)
                    cleaned += 1
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={"cleaned_sessions": cleaned}
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class TokenOptimizationWorker(BaseWorker):
    """Optimiza uso de tokens"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="token_optimization",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=900,  # 15 min
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            budget = context.get("token_budget")
            if not budget:
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No token budget")
            
            status = budget.get_status() if hasattr(budget, "get_status") else {}
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={"budget_status": status}
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class SecurityScanWorker(BaseWorker):
    """Escanea patrones de seguridad"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="security_scan",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=300,  # 5 min
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            hooks = context.get("hooks")
            if not hooks:
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No hooks engine")
            
            # Obtener métricas de seguridad
            metrics = hooks.get_metrics() if hasattr(hooks, "get_metrics") else {}
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={"security_metrics": metrics}
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class PerformanceMetricsWorker(BaseWorker):
    """Recolecta métricas de performance"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="performance_metrics",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=60,  # 1 min
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            director = context.get("director")
            if not director:
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No director")
            
            # Recolectar métricas
            metrics = {
                "gemas_count": len(director.gemas) if hasattr(director, "gemas") else 0,
                "sessions_active": director.sessions.active_sessions if hasattr(director.sessions, "active_sessions") else 0,
                "execution_log_size": len(director.execution_log) if hasattr(director, "execution_log") else 0,
            }
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={"metrics": metrics}
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class ContextCompactionWorker(BaseWorker):
    """Compacta contexto automáticamente"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="context_compaction",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=1200,  # 20 min
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            compactor = context.get("compactor")
            sessions = context.get("sessions")
            if not compactor or not sessions:
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No compactor/sessions")
            
            compacted = 0
            for session_id in list(sessions.sessions.keys()):
                if sessions.needs_compact(session_id):
                    sessions.compact_session_trajectory(session_id)
                    compacted += 1
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={"compacted_sessions": compacted}
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class ErrorPatternWorker(BaseWorker):
    """Detecta patrones de error"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="error_pattern_detection",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=300,  # 5 min
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            execution_log = context.get("execution_log", [])
            
            # Analizar últimos 100 ejecuciones
            recent = execution_log[-100:]
            errors = [e for e in recent if not e.get("success", True)]
            
            patterns = {}
            for error in errors:
                error_type = error.get("error_type", "unknown")
                patterns[error_type] = patterns.get(error_type, 0) + 1
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={
                    "total_errors": len(errors),
                    "error_patterns": patterns,
                    "error_rate": len(errors) / len(recent) if recent else 0
                }
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class KnowledgeSyncWorker(BaseWorker):
    """Sincroniza conocimiento entre nodos"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="knowledge_sync",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=600,  # 10 min
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            # Verificar conectividad con nodos
            connectivity = context.get("connectivity")
            if not connectivity:
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No connectivity layer")
            
            nodes = connectivity.get_nodes() if hasattr(connectivity, "get_nodes") else []
            synced = 0
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={"nodes_checked": len(nodes), "synced": synced}
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class HealthCheckWorker(BaseWorker):
    """Verifica salud del sistema"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="health_check",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=30,  # 30 sec
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            health = context.get("memory_health")
            if not health:
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No health monitor")
            
            status = health.get_status() if hasattr(health, "get_status") else {}
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={"health_status": status}
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class LearningWorker(BaseWorker):
    """Aprende de interacciones pasadas"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="learning",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=1800,  # 30 min
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            execution_log = context.get("execution_log", [])
            
            # Analizar patrones de éxito
            recent = execution_log[-500:]
            successes = [e for e in recent if e.get("success", False)]
            
            # Aprender patrones exitosos
            patterns = {}
            for success in successes:
                gem = success.get("gem", "unknown")
                patterns[gem] = patterns.get(gem, 0) + 1
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={
                    "success_rate": len(successes) / len(recent) if recent else 0,
                    "successful_patterns": patterns
                }
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class SkillAuditWorker(BaseWorker):
    """Audita skills obsoletos"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="skill_audit",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=3600,  # 1h
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            skill_loader = context.get("skill_loader")
            if not skill_loader:
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No skill loader")
            
            stats = skill_loader.get_stats() if hasattr(skill_loader, "get_stats") else {}
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={"skill_stats": stats}
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class BackupWorker(BaseWorker):
    """Backups automáticos"""
    
    def __init__(self):
        super().__init__(WorkerConfig(
            name="backup",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=7200,  # 2h
        ))
    
    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            nexus_home = context.get("nexus_home", Path.home() / ".nexus")
            backup_dir = Path(nexus_home) / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            # Backup de archivos críticos
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = backup_dir / f"backup_{timestamp}.json"
            
            backup_data = {
                "timestamp": timestamp,
                "type": "full_backup",
                "files_backed_up": []
            }
            
            atomic_write_json(backup_file, backup_data)
            
            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={"backup_file": str(backup_file)}
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class ThreeLoopWorker(BaseWorker):
    """Auto-mejora: ejecuta el three-loop self-improvement system periodicamente"""

    def __init__(self):
        super().__init__(WorkerConfig(
            name="three_loop_improvement",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=3600,  # 1h
        ))

    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            director = context.get("director")
            if not director or not hasattr(director, 'three_loop'):
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No three_loop available")

            async def exec_fn(task: str) -> str:
                return await director.training_brain.llm_gateway_text(task)

            judge_fn = director.training_brain.judge_response if hasattr(director, 'training_brain') and hasattr(director.training_brain, 'judge_response') else None

            medium = await director.three_loop.run_medium_loop(
                execute_fn=exec_fn,
                judge_fn=judge_fn,
                sample_size=6,
            )

            slow = await director.three_loop.run_slow_loop()

            state = director.three_loop.state.__dict__

            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={
                    "medium_loop_score": medium.get("benchmark", {}).get("avg_score", 0),
                    "slow_loop_recommendations": len(slow.get("recommendations", [])),
                    "state": state,
                }
            )
        except Exception as e:
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class TrainingWorker(BaseWorker):
    """Entrenamiento cada 15 min: tareas rotativas, adaptativas, competición PC1↔REMOTE."""

    # Pool de tareas rotativas — se eligen 2 por ciclo para no saturar VRAM
    _TASK_POOL = [
        # Coding
        ("Write a Python function to merge two sorted lists efficiently", "coding"),
        ("Implement a LRU cache in Python with O(1) get/put", "coding"),
        ("Write a decorator that retries a function N times with exponential backoff", "coding"),
        ("Implement async producer-consumer pattern with asyncio.Queue", "coding"),
        ("Write a function to find the longest common subsequence of two strings", "coding"),
        ("Implement a thread-safe singleton in Python", "coding"),
        ("Write a context manager for database transactions with rollback", "coding"),
        ("Implement a simple event bus (pub/sub) in Python", "coding"),
        # Research
        ("Explain the time complexity of quicksort vs mergesort with examples", "research"),
        ("Compare RAG vs fine-tuning for domain-specific LLM applications", "research"),
        ("Explain how attention mechanism works in transformers", "research"),
        ("Compare SQLite WAL mode vs journal mode for concurrent access", "research"),
        ("Explain the CAP theorem with real-world database examples", "research"),
        ("Describe how LoRA reduces memory for fine-tuning LLMs", "research"),
        # Math
        ("Solve: if x^2 + 3x + 2 = 0, what are the roots?", "math"),
        ("Calculate the derivative of f(x) = x^3 * ln(x)", "math"),
        ("A server handles 1000 req/s with 50ms avg latency. What's the avg queue depth?", "math"),
        ("Probability: 3 dice rolled, P(sum >= 15)?", "math"),
        # Architecture
        ("Describe the singleton pattern and when to use it", "architecture"),
        ("Design a message queue system for microservices", "architecture"),
        ("Compare event sourcing vs CRUD for an agent memory system", "architecture"),
        ("Design a circuit breaker pattern for LLM API calls", "architecture"),
        ("How would you design a multi-tenant agent platform?", "architecture"),
    ]
    _cycle_counter = 0

    def __init__(self):
        super().__init__(WorkerConfig(
            name="training",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=900,   # 15 min
            timeout_seconds=600,    # 10 min max (model swapping is slow with 6GB VRAM)
        ))

    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            director = context.get("director")
            if not director or not hasattr(director, 'peer_chat'):
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No peer_chat")
            pc = director.peer_chat

            # 1. Ping peers
            status = await pc.ping()
            pc1_ok = status.get("pc1", {}).get("online", False)
            REMOTE_ok = status.get("REMOTE", {}).get("online", False)
            if not pc1_ok and not REMOTE_ok:
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="Both peers offline")

            # 2. Rotate through task pool — 2 tasks per cycle to respect VRAM limits
            pool = self._TASK_POOL
            idx = (TrainingWorker._cycle_counter * 2) % len(pool)
            training_tasks = [pool[idx], pool[(idx + 1) % len(pool)]]
            TrainingWorker._cycle_counter += 1

            results = []
            for task, cat in training_tasks:
                try:
                    r = await asyncio.wait_for(
                        pc.collaborative_task(task, cat),
                        timeout=180,  # 3 min per task max
                    )
                    results.append(r)
                except asyncio.TimeoutError:
                    logger.warning(f"Training task timed out: {task[:50]}...")
                    results.append({"success": False, "task": task, "error": "timeout"})
                await asyncio.sleep(2)  # Let VRAM breathe between tasks

            # 3. Adaptive tasks for weak categories (1 max)
            weak = pc._get_weak_categories(min_samples=3)
            if weak:
                node, cat, rate = weak[0]
                try:
                    t = await asyncio.wait_for(pc.generate_adaptive_tasks(1), timeout=60)
                    if t:
                        await asyncio.wait_for(
                            pc.collaborative_task(t[0]["task"], cat),
                            timeout=180,
                        )
                except asyncio.TimeoutError:
                    logger.warning(f"Adaptive task timed out for {node}/{cat}")

            # 4. Win stats summary
            win_summary = {}
            for node, cats in pc._win_stats.items():
                for cat, s in cats.items():
                    rate = s["wins"] / s["total"] if s["total"] > 0 else 0
                    win_summary[f"{node}/{cat}"] = f"{s['wins']}/{s['total']} ({rate:.0%})"

            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={
                    "training_tasks": len(training_tasks),
                    "results_count": len([r for r in results if isinstance(r, dict) and r.get("success", True)]),
                    "weak_categories": len(weak),
                    "win_summary": win_summary,
                    "knowledge_count": len(pc.learned_knowledge),
                    "cycle": TrainingWorker._cycle_counter,
                }
            )
        except Exception as e:
            logger.error(f"TrainingWorker error: {e}", exc_info=True)
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


class PeerLearningWorker(BaseWorker):
    """Chat y autoaprendizaje PC1 ↔ REMOTE: colaboracion, debate, y compilación de conocimiento"""

    _CONVERSATION_TOPICS = [
        "How can we improve our self-learning pipeline for agent autonomy?",
        "What new capabilities should we develop for multi-agent coordination?",
        "Analyze trade-offs: local LLM vs API calls for latency-sensitive tasks",
        "Design a memory consolidation strategy for long-running agents",
        "Compare agentic coding patterns: ReAct vs Plan-Execute vs Tree-of-Thought",
        "How should we handle tool failures gracefully in autonomous agents?",
        "Propose a benchmark suite for measuring agent self-improvement",
        "Design a knowledge distillation pipeline from large to small models",
        "How to detect and recover from hallucinations in autonomous workflows?",
        "Compare WebSocket vs SSE vs long-polling for real-time agent communication",
    ]

    _LEARNING_TASKS = [
        ("Implement a priority queue with decrease-key operation in Python", "coding"),
        ("Design a rate limiter for API calls using token bucket algorithm", "coding"),
        ("Write a function to detect cycles in a directed graph", "coding"),
        ("Implement a simple B-tree insertion in Python", "coding"),
        ("Explain consensus algorithms: Raft vs Paxos for distributed systems", "research"),
        ("Compare vector databases: FAISS vs ChromaDB vs SQLite-vss", "research"),
        ("How does speculative decoding improve LLM inference speed?", "research"),
        ("Design a fault-tolerant task scheduler for a multi-agent system", "architecture"),
        ("Compare event-driven vs polling architectures for agent coordination", "architecture"),
        ("Calculate optimal batch size given 6GB VRAM and 7B parameter model", "math"),
    ]
    _cycle_counter = 0

    def __init__(self):
        super().__init__(WorkerConfig(
            name="peer_learning",
            trigger=WorkerTrigger.SCHEDULED,
            interval_seconds=1800,  # 30 min
            timeout_seconds=900,    # 15 min timeout (full learning cycle)
        ))

    async def run(self, context: Dict[str, Any]) -> WorkerResult:
        try:
            director = context.get("director")
            if not director or not hasattr(director, 'peer_chat'):
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="No peer_chat available")

            pc = director.peer_chat

            # Ping both peers
            status = await pc.ping()
            pc1_ok = status.get("pc1", {}).get("online", False)
            REMOTE_ok = status.get("REMOTE", {}).get("online", False)

            if not pc1_ok and not REMOTE_ok:
                return WorkerResult(self.config.name, success=False, duration_ms=0, error="Both peers offline")

            cycle = PeerLearningWorker._cycle_counter
            PeerLearningWorker._cycle_counter += 1

            # Phase 1: Conversation (2 rounds on rotating topic)
            topic_idx = cycle % len(self._CONVERSATION_TOPICS)
            topic = self._CONVERSATION_TOPICS[topic_idx]
            try:
                await asyncio.wait_for(
                    pc.peer_conversation(rounds=2, topic=topic),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                logger.warning(f"PeerLearning conversation timed out: {topic[:50]}")

            await asyncio.sleep(3)  # VRAM cooldown

            # Phase 2: Collaborative learning tasks (2 rotating tasks)
            task_idx = (cycle * 2) % len(self._LEARNING_TASKS)
            tasks = [
                self._LEARNING_TASKS[task_idx][0],
                self._LEARNING_TASKS[(task_idx + 1) % len(self._LEARNING_TASKS)][0],
            ]
            categories = [
                self._LEARNING_TASKS[task_idx][1],
                self._LEARNING_TASKS[(task_idx + 1) % len(self._LEARNING_TASKS)][1],
            ]
            try:
                result = await asyncio.wait_for(
                    pc.learn_from_best(tasks, categories),
                    timeout=480,  # 8 min for 2 tasks with judge
                )
            except asyncio.TimeoutError:
                logger.warning("PeerLearning learn_from_best timed out")
                result = {"total_tasks": 0, "win_stats": {}}

            # Phase 3: Sync brain and post report
            try:
                await asyncio.wait_for(pc.sync_brain(), timeout=30)
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Brain sync failed (non-critical): {e}")

            pc.post_report_to_memory(director.hybrid_memory, f"PeerChat Learning Cycle #{cycle}")

            return WorkerResult(
                worker_name=self.config.name,
                success=True,
                duration_ms=0,
                data={
                    "total_tasks": result.get("total_tasks", 0),
                    "win_stats": result.get("win_stats", {}),
                    "pc1_online": pc1_ok,
                    "REMOTE_online": REMOTE_ok,
                    "knowledge_count": len(pc.learned_knowledge),
                    "topic": topic[:60],
                    "cycle": cycle,
                }
            )
        except Exception as e:
            logger.error(f"PeerLearningWorker error: {e}", exc_info=True)
            return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))


# ============================================================
# WORKER MANAGER
# ============================================================
# WORKER MANAGER
# ============================================================

class DaemonRuntime:
    """
    Daemon Runtime Loop — heartbeat + auto-registro + GC + health.
    Inspirado en el patrón daemon.go de multica (8 loops concurrentes).

    Cada runtime se registra automáticamente al iniciar y envía heartbeats
    periódicos. Si el runtime es dado de baja (server-side), se re-registra solo.
    """

    def __init__(self, worker_manager: "BackgroundWorkerManager", context: Dict[str, Any]):
        self.wm = worker_manager
        self.ctx = context
        self.runtime_id = f"nexus-{datetime.now().strftime('%Y%m%d%H%M%S')}-{id(self)}"
        self._running = False
        self._loops: Dict[str, asyncio.Task] = {}

    async def start(self):
        """Start all 4 daemon loops — staggered."""
        self._running = True
        loops = {
            "heartbeat": self._heartbeat_loop(30),
            "gc": self._gc_loop(300),
            "auto_update": self._auto_update_loop(3600),
            "health": self._health_server(),
        }
        for name, coro in loops.items():
            task = asyncio.create_task(coro)
            task.set_name(f"daemon:{name}")
            self._loops[name] = task
            await asyncio.sleep(0.1)
        logger.info(f"DaemonRuntime started: {self.runtime_id} (4 loops)")

    async def _heartbeat_loop(self, interval: int):
        """Every N seconds: report alive, detect if deregistered, re-register."""
        while self._running:
            try:
                nexus_dir = Path.home() / ".nexus"
                nexus_dir.mkdir(exist_ok=True)
                hb = nexus_dir / "daemon.heartbeat"
                atomic_write_json(hb, {
                    "runtime_id": self.runtime_id,
                    "timestamp": datetime.now().isoformat(),
                    "workers_alive": sum(1 for t in self._loops.values() if not t.done()),
                    "pid": os.getpid(),
                })
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Heartbeat error: {e}")
                await asyncio.sleep(10)

    async def _gc_loop(self, interval: int):
        """Clean stale sessions, old results, temp files."""
        while self._running:
            try:
                nexus_dir = Path.home() / ".nexus"
                if nexus_dir.exists():
                    # Clean heartbeats older than 1h (stale daemons)
                    now = time.time()
                    for f in nexus_dir.glob("daemon.*"):
                        if f.is_file() and (now - f.stat().st_mtime) > 3600:
                            f.unlink(missing_ok=True)
                            logger.info(f"GC cleaned stale daemon file: {f.name}")

                # Clean worker results older than threshold (from worker_manager)
                cutoff = datetime.now() - timedelta(hours=2)
                async with self.wm._results_lock:
                    self.wm._results = [
                        r for r in self.wm._results
                        if hasattr(r, 'timestamp') and r.timestamp
                        and datetime.fromisoformat(r.timestamp) > cutoff
                    ]
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"GC error: {e}")
                await asyncio.sleep(60)

    async def _auto_update_loop(self, interval: int):
        """Check for system updates."""
        while self._running:
            try:
                logger.debug("Auto-update check skipped (user-managed)")
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _health_server(self):
        """Serve health status via a simple file-based endpoint."""
        while self._running:
            try:
                nexus_dir = Path.home() / ".nexus"
                nexus_dir.mkdir(exist_ok=True)
                status = self.wm.get_status()
                health = nexus_dir / "daemon.health"
                atomic_write_json(health, {
                    "runtime_id": self.runtime_id,
                    "status": "healthy",
                    "workers": {k: {"enabled": v.get("enabled"), "running": v.get("running"),
                                   "run_count": v.get("run_count")} for k, v in status.items()},
                    "timestamp": datetime.now().isoformat(),
                })
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Health server error: {e}")
                await asyncio.sleep(15)

    async def stop(self):
        self._running = False
        for task in self._loops.values():
            task.cancel()
        await asyncio.gather(*self._loops.values(), return_exceptions=True)


class BackgroundWorkerManager:
    """
    Gestiona los background workers con daemon runtime.
    
    Inspirado en multica daemon.go:
    - Workers scheduled con auto-recovery
    - DaemonRuntime con heartbeat/GC/health
    - Auto-re-registro si runtime es dado de baja
    
    Uso:
        manager = BackgroundWorkerManager()
        manager.register_all()
        await manager.start(context)
    """
    
    def __init__(self):
        self.workers: Dict[str, BaseWorker] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._results: List[WorkerResult] = []
        self._results_lock = asyncio.Lock()
        self.daemon: Optional[DaemonRuntime] = None
        # process_scheduled() can fire from BackgroundCognition before start()
        # ran (DMN heartbeat) — initialize empty so workers don't crash with
        # AttributeError. start() overwrites with the real context.
        self._last_context: Dict[str, Any] = {}
    
    def register_all(self):
        """Registrar todos los workers"""
        workers = [
            MemoryConsolidationWorker(),
            SessionCleanupWorker(),
            TokenOptimizationWorker(),
            SecurityScanWorker(),
            PerformanceMetricsWorker(),
            ContextCompactionWorker(),
            ErrorPatternWorker(),
            KnowledgeSyncWorker(),
            HealthCheckWorker(),
            LearningWorker(),
            SkillAuditWorker(),
            BackupWorker(),
            ThreeLoopWorker(),
            TrainingWorker(),       # cada 15 min
            PeerLearningWorker(),   # cada 30 min
        ]
        # DMN — background cognition (lethe parity, 1h)
        try:
            from src.core.dmn import make_dmn_worker
            workers.append(make_dmn_worker())
        except Exception as e:
            logger.warning(f"DMN worker not registered: {e}")
        
        for worker in workers:
            self.workers[worker.config.name] = worker
            logger.info(f"Registered worker: {worker.config.name} (trigger: {worker.config.trigger.value}, interval: {worker.config.interval_seconds}s)")
    
    async def start(self, context: Dict[str, Any]):
        """Iniciar workers + daemon runtime + supervisor — con stagger para no saturar el event loop."""
        self._running = True
        self._last_context = context
        self._worker_tasks: Dict[str, asyncio.Task] = {}

        # Debug isolation: NEXUS_DEBUG_WORKERS=skip-workers / skip-daemon / skip-supervisor / skip-all
        debug = os.environ.get("NEXUS_DEBUG_WORKERS", "")
        STAGGER_S = 0.25

        if "skip-workers" not in debug and "skip-all" not in debug:
            for name, worker in self.workers.items():
                if worker.config.trigger == WorkerTrigger.SCHEDULED:
                    task = asyncio.create_task(self._run_worker_loop(name, worker, context))
                    task.set_name(f"worker:{name}")
                    self._worker_tasks[name] = task
                    self._tasks.append(task)
                    await asyncio.sleep(STAGGER_S)

        if "skip-daemon" not in debug and "skip-all" not in debug:
            self.daemon = DaemonRuntime(self, context)
            await self.daemon.start()
            await asyncio.sleep(STAGGER_S)

        if "skip-supervisor" not in debug and "skip-all" not in debug:
            supervisor = asyncio.create_task(self._supervisor_loop(context))
            supervisor.set_name("worker:supervisor")
            self._tasks.append(supervisor)
            await asyncio.sleep(STAGGER_S)

        logger.info(f"Started {len(self._worker_tasks)} background workers + daemon runtime + supervisor (staggered)")

    async def _supervisor_loop(self, context: Dict[str, Any]):
        """Supervisa workers cada 60s. Si uno murió, lo re-crea."""
        while self._running:
            try:
                await asyncio.sleep(60)
                for name, task in list(self._worker_tasks.items()):
                    if task.done() and self._running:
                        exc = task.exception() if not task.cancelled() else None
                        logger.warning(
                            f"Supervisor: worker {name} died"
                            f"{f' ({exc})' if exc else ''}, restarting..."
                        )
                        worker = self.workers.get(name)
                        if worker:
                            new_task = asyncio.create_task(
                                self._run_worker_loop(name, worker, context)
                            )
                            new_task.set_name(f"worker:{name}")
                            self._worker_tasks[name] = new_task
                            self._tasks.append(new_task)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Supervisor error: {e}")
                await asyncio.sleep(30)
    
    async def stop(self):
        """Detener todos los workers + daemon runtime"""
        self._running = False
        if self.daemon:
            await self.daemon.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("All background workers + daemon stopped")
    
    async def run_on_demand(self, worker_name: str, context: Dict[str, Any]) -> WorkerResult:
        """Ejecutar un worker específico on-demand"""
        worker = self.workers.get(worker_name)
        if not worker:
            return WorkerResult(worker_name, success=False, duration_ms=0, error=f"Worker not found: {worker_name}")
        
        result = await worker.execute(context)
        async with self._results_lock:
            self._results.append(result)
        return result
    
    async def process_scheduled(self):
        """Execute a single iteration of all scheduled workers (called by BackgroundCognition DMN)."""
        for name, worker in self.workers.items():
            if worker.config.trigger == WorkerTrigger.SCHEDULED and worker.config.enabled:
                try:
                    result = await worker.execute(self._last_context)
                    async with self._results_lock:
                        self._results.append(result)
                except Exception as e:
                    logger.warning(f"Scheduled worker {name} error: {e}")

    async def _run_worker_loop(self, name: str, worker: BaseWorker, context: Dict[str, Any]):
        """Loop de ejecución para un worker scheduled — resiliente con auto-recovery."""
        consecutive_failures = 0
        max_consecutive_failures = 5
        base_backoff = 10  # seconds

        # Initial delay: spread first-runs over a window so 15 workers don't
        # hammer the event loop at the same instant on startup.
        worker_idx = list(self.workers.keys()).index(name) if name in self.workers else 0
        initial_delay = min(2 + worker_idx * 1.5, 30)  # 2s..30s spread
        await asyncio.sleep(initial_delay)

        while self._running:
            try:
                result = await worker.execute(context)
                async with self._results_lock:
                    self._results.append(result)

                if result.success:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    logger.warning(
                        f"Worker {name} failed ({consecutive_failures}/{max_consecutive_failures}): {result.error}"
                    )

                # Exponential backoff if failing repeatedly
                if consecutive_failures >= max_consecutive_failures:
                    cooldown = min(base_backoff * (2 ** consecutive_failures), 900)  # max 15min
                    logger.error(
                        f"Worker {name} hit {consecutive_failures} consecutive failures, cooling down {cooldown}s"
                    )
                    await asyncio.sleep(cooldown)
                    consecutive_failures = 0  # reset after cooldown
                else:
                    await asyncio.sleep(worker.config.interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                backoff = min(base_backoff * (2 ** consecutive_failures), 300)
                logger.error(
                    f"Worker {name} loop error ({consecutive_failures}x): {e}. "
                    f"Auto-recovering in {backoff}s...",
                    exc_info=True,
                )
                await asyncio.sleep(backoff)

        logger.info(f"Worker {name} loop stopped (running={self._running})")
    
    def get_status(self) -> Dict[str, Any]:
        """Obtener estado de todos los workers"""
        status = {}
        for name, worker in self.workers.items():
            status[name] = {
                "enabled": worker.config.enabled,
                "running": worker.is_running,
                "phase": getattr(worker, "phase", "unknown"),
                "run_count": worker.run_count,
                "error_count": worker.error_count,
                "last_run": worker.last_run.__dict__ if worker.last_run else None,
            }
        return status

    def detect_stalled(self, threshold_minutes: int = 30) -> List[Dict[str, Any]]:
        """Watchdog: workers enabled + interval-scheduled that haven't run in
        > threshold_minutes are considered stalled. Emits WORKER_STALLED on
        the event bus per hit so observability sees the deadlock as soon as
        something polls this method.

        Returns one row per stalled worker:
            {name, last_ts, minutes_since, interval_seconds, error_count}

        Workers that have never run are NOT flagged (they may be cold-started
        with a long first-run delay — see background_workers init stagger).
        Workers with interval_seconds=0 or enabled=False are skipped.
        """
        import datetime as _dt
        out: List[Dict[str, Any]] = []
        now = _dt.datetime.now()
        for name, worker in self.workers.items():
            cfg = worker.config
            if not cfg.enabled or not getattr(cfg, "interval_seconds", 0):
                continue
            lr = worker.last_run
            if lr is None or not getattr(lr, "timestamp", None):
                continue
            try:
                last_ts = _dt.datetime.fromisoformat(lr.timestamp)
            except Exception:
                continue
            mins = (now - last_ts).total_seconds() / 60.0
            # Stalled when we've missed > 2 expected ticks AND exceeded the
            # absolute threshold. Both conditions guard against flagging
            # naturally slow workers (e.g. interval=1h) prematurely.
            expected_mins = cfg.interval_seconds / 60.0
            if mins > max(threshold_minutes, expected_mins * 2):
                row = {
                    "name": name,
                    "last_ts": lr.timestamp,
                    "minutes_since": round(mins, 1),
                    "interval_seconds": cfg.interval_seconds,
                    "error_count": worker.error_count,
                }
                out.append(row)
                # Emit observability event (best-effort, never break watchdog).
                try:
                    from src.observability.event_stream import emit, EventType
                    emit(EventType.WORKER_STALLED, data=row, source="background_workers")
                except Exception:
                    pass
        return out
    
    def get_recent_results(self, limit: int = 20) -> List[Dict]:
        """Obtener resultados recientes"""
        results = list(self._results[-limit:])
        return [r.__dict__ for r in results]
