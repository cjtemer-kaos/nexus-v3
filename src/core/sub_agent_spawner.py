"""
Sub-Agent Spawner - Delegacion de tareas a sub-agentes para SuperNEXUS v2

Crea sub-agentes dinamicos para tareas complejas que pueden descomponerse.

Caracteristicas:
- ThreadPool dinamico con depth limit MAX=3
- Cada sub-agente tiene contexto aislado
- Resultados se agregan al agente padre
- Timeout y cancellation por sub-agente
- Resource limits por sub-agente
- Active Tracking: Begin/Active pattern for UI visibility

"""

import asyncio
import hashlib
import inspect
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("nexus-subagent")

#: Providers de spawn soportados (PASO 3).
CHILD_PROVIDERS = ("spawn", "fork", "gema", "acp")

#: Modos de vida del hijo (PASO 3).
CHILD_MODES = ("one-shot", "continuable")


def stable_child_id(task: str, parent_id: str = "", depth: int = 1, provider: str = "spawn", mode: str = "one-shot") -> str:
    """child_id estable: mismos inputs → mismo id (re-ejecuciones idempotentes)."""
    raw = f"{parent_id}|{depth}|{provider}|{mode}|{task}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:8]


class SubAgentStatus(Enum):
    PENDING = "pending"
    BEGIN = "begin"      # New: Just created, not yet running
    ACTIVE = "active"    # New: Actively working (UI visible)
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class SubAgentResult:
    """Resultado de un sub-agente"""
    agent_id: str
    task: str
    status: SubAgentStatus
    result: Any = None
    error: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration_ms: float = 0
    tokens_used: int = 0
    # Active Tracking fields
    begin_time: str = ""      # When sub-agent was created
    active_time: str = ""     # When sub-agent started working
    progress: str = ""        # Current progress description
    parent_id: str = ""       # Parent agent ID for hierarchy
    # PASO 3: identidad y linaje del hijo
    child_id: str = ""            # id estable (== agent_id en hijos nuevos)
    provider: str = "spawn"       # spawn|fork|gema|acp
    mode: str = "one-shot"        # one-shot|continuable
    depth: int = 1                # delegationDepth
    child_session_id: str = ""    # sesión hija aislada
    inbox: List[str] = field(default_factory=list)  # mensajes via send_message


@dataclass
class SubAgentSpec:
    """Especificacion de un sub-agente"""
    task: str
    gem: str = "auto"  # Gema asignada
    context: str = ""
    timeout_seconds: float = 300
    max_retries: int = 0
    priority: int = 1  # 1-5, 5 = mas prioritario
    # PASO 3 (todos con default: compatibilidad total)
    provider: str = "spawn"       # spawn|fork|gema|acp
    mode: str = "one-shot"        # one-shot|continuable
    descriptor: str = ""          # log-only: se registra, NUNCA se ejecuta
    scope_roots: Any = None       # raíces pedidas; se intersectan con el scope padre (nunca se ensancha)
    # Subconjunto de tools del hijo. Vacío = todas las del padre. Al estrechar
    # el set se reduce superficie de error y costo de contexto por sub-agente.
    allowed_tools: Any = ()       # tuple[str, ...] | list[str]


class SubAgentSpawner:
    """
    Gestor de sub-agentes con delegacion dinamica.

    Uso:
        spawner = SubAgentSpawner(executor=director.execute)
        results = await spawner.spawn_parallel([
            SubAgentSpec(task="analizar codigo", gem="code"),
            SubAgentSpec(task="buscar documentacion", gem="scholar"),
        ])
    """

    MAX_DEPTH = 3  # Limite maximo de delegacion en cascada
    MAX_CONCURRENT = 5  # Maximo sub-agentes concurrentes

    def __init__(
        self,
        executor: Callable = None,
        max_depth: int = MAX_DEPTH,
        max_concurrent: int = MAX_CONCURRENT,
        scope_roots: Any = None,
        session_log=None,
        parent_session_id: str = "",
    ):
        self.executor = executor
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent
        # PASO 3: scope padre (solo se puede estrechar, nunca ensanchar).
        self.scope_roots = frozenset(scope_roots) if scope_roots else frozenset()
        self.session_log = session_log
        self.parent_session_id = parent_session_id
        self._active_agents: Dict[str, SubAgentResult] = {}
        # PASO 3: registro de hijos (persiste para continuable) + buzones + interrupciones.
        self._children: Dict[str, SubAgentResult] = {}
        self._inbox: Dict[str, List[str]] = {}
        self._interrupted: set[str] = set()
        self._child_scopes: Dict[str, frozenset] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._stats = {
            "total_spawned": 0,
            "total_completed": 0,
            "total_failed": 0,
            "total_cancelled": 0,
            "total_timeout": 0,
            "total_duration_ms": 0,
        }
        # Active Tracking callback (for UI updates)
        self._on_status_change: Optional[Callable] = None

    def _effective_scope(self, spec: SubAgentSpec) -> frozenset:
        """Scope del hijo = intersección con el padre. Sin scope padre o sin
        pedido extra, hereda el padre tal cual. NUNCA se ensancha."""
        if not self.scope_roots:
            return frozenset(spec.scope_roots) if spec.scope_roots else frozenset()
        if not spec.scope_roots:
            return self.scope_roots
        return self.scope_roots & frozenset(spec.scope_roots)

    def _child_session_id(self, child_id: str) -> str:
        """Sesión hija aislada: namespace propio derivado del padre."""
        base = self.parent_session_id or "root"
        return f"{base}::child::{child_id}"

    def _ensure_child_session(self, child_session_id: str) -> None:
        """Crea la sesión hija en SessionLog (solo API pública). Best-effort."""
        if self.session_log is None or not child_session_id:
            return
        try:
            if self.session_log.get_session(child_session_id) is None:
                self.session_log.create_session(project="subagent", session_id=child_session_id)
        except Exception as e:
            logger.debug(f"child session no creada: {e}")

    # -- PASO 3: mensajería / interrupción / listado --------------------
    def send_message(self, child_id: str, message: str) -> bool:
        """Encola un mensaje al buzón del hijo. False si no existe."""
        if child_id not in self._children:
            return False
        self._inbox.setdefault(child_id, []).append(message)
        self._children[child_id].inbox.append(message)
        return True

    def drain_inbox(self, child_id: str) -> List[str]:
        """Saca y devuelve los mensajes pendientes del hijo (mailbox read)."""
        msgs = list(self._inbox.get(child_id, []) or [])
        self._inbox[child_id] = []
        rec = self._children.get(child_id)
        if rec is not None:
            rec.inbox = []
        return msgs

    def interrupt(self, child_id: str) -> bool:
        """Interrumpe un hijo (best-effort): marca flag y estado CANCELLED."""
        if child_id not in self._children:
            return False
        self._interrupted.add(child_id)
        rec = self._children[child_id]
        rec.status = SubAgentStatus.CANCELLED
        if child_id in self._active_agents:
            self._active_agents[child_id].status = SubAgentStatus.CANCELLED
        return True

    def list_children(self) -> List[Dict[str, Any]]:
        """Lista hijos conocidos (activos + terminados)."""
        return [
            {
                "child_id": cid,
                "task": r.task,
                "status": r.status.value,
                "provider": r.provider,
                "mode": r.mode,
                "depth": r.depth,
                "parent_id": r.parent_id,
                "child_session_id": r.child_session_id,
                "inbox_len": len(self._inbox.get(cid, [])),
            }
            for cid, r in self._children.items()
        ]

    def get_child(self, child_id: str) -> Optional[SubAgentResult]:
        return self._children.get(child_id)

    def child_scope(self, child_id: str) -> frozenset:
        return self._child_scopes.get(child_id, frozenset())

    def for_child(self, child_id: str) -> "SubAgentSpawner":
        """Spawner anidado para que un hijo delegue: depth+1 y scope estrechado.

        Comparte executor/registry con el padre (mismo loop), pero el scope
        del nieto nunca excede el del hijo.
        """
        rec = self._children.get(child_id)
        base_depth = (rec.depth if rec else 1)
        child_scope = self._child_scopes.get(child_id, self.scope_roots)
        nested = SubAgentSpawner(
            executor=self.executor,
            max_depth=self.max_depth,
            max_concurrent=self.max_concurrent,
            scope_roots=child_scope,
            session_log=self.session_log,
            parent_session_id=rec.child_session_id if rec else self.parent_session_id,
        )
        # Comparte el registro para que list_children del padre vea los nietos.
        nested._children = self._children
        nested._inbox = self._inbox
        nested._interrupted = self._interrupted
        nested._child_scopes = self._child_scopes
        nested._base_depth = base_depth + 1  # type: ignore[attr-defined]
        return nested

    async def spawn_child(self, spec: SubAgentSpec, depth: int = 1, parent_id: str = "") -> SubAgentResult:
        """Spawn de un único hijo (atajo sobre _run_subagent)."""
        base = getattr(self, "_base_depth", None)
        if base is not None:
            depth = max(depth, base)  # type: ignore[arg-type]
        return await self._run_subagent(spec, depth, parent_id)

    def set_status_callback(self, callback: Callable):
        """Set callback for status changes (UI visibility)."""
        self._on_status_change = callback

    def _notify_status_change(self, agent_id: str, status: SubAgentStatus, progress: str = ""):
        """Notify UI of status change (Begin/Active pattern)."""
        if self._on_status_change:
            try:
                self._on_status_change(agent_id, status, progress)
            except Exception as e:
                logger.warning(f"Status callback failed: {e}")

    async def spawn_parallel(self, specs: List[SubAgentSpec], depth: int = 1) -> List[SubAgentResult]:
        """
        Ejecuta multiples sub-agentes en paralelo.

        Args:
            specs: Lista de especificaciones de sub-agentes
            depth: Nivel de delegacion actual (para evitar recursion infinita)

        Returns:
            Lista de resultados de sub-agentes
        """
        if depth > self.max_depth:
            logger.warning(f"Max delegation depth reached ({self.max_depth}), aborting")
            return [
                SubAgentResult(
                    agent_id="depth_limit",
                    task=spec.task,
                    status=SubAgentStatus.FAILED,
                    error=f"Max delegation depth exceeded ({self.max_depth})",
                )
                for spec in specs
            ]

        if not self.executor:
            logger.error("No executor configured for sub-agent spawning")
            return [
                SubAgentResult(
                    agent_id="no_executor",
                    task=spec.task,
                    status=SubAgentStatus.FAILED,
                    error="No executor configured",
                )
                for spec in specs
            ]

        # Ordenar por prioridad (mayor primero)
        sorted_specs = sorted(specs, key=lambda s: s.priority, reverse=True)

        # Ejecutar en paralelo con semaphore
        tasks = [self._run_subagent(spec, depth) for spec in sorted_specs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Procesar resultados
        final_results = []
        for r in results:
            if isinstance(r, Exception):
                final_results.append(SubAgentResult(
                    agent_id="error",
                    task="",
                    status=SubAgentStatus.FAILED,
                    error=str(r),
                ))
            else:
                final_results.append(r)

        return final_results

    async def spawn_sequential(self, specs: List[SubAgentSpec], depth: int = 1) -> List[SubAgentResult]:
        """
        Ejecuta sub-agentes secuencialmente (cada uno puede usar resultado del anterior).

        Args:
            specs: Lista de especificaciones
            depth: Nivel de delegacion

        Returns:
            Lista de resultados en orden
        """
        results = []
        for spec in specs:
            result = await self._run_subagent(spec, depth)
            results.append(result)
            # Si fallo y no hay retries, detener
            if result.status == SubAgentStatus.FAILED and spec.max_retries == 0:
                break
        return results

    async def _run_subagent(self, spec: SubAgentSpec, depth: int, parent_id: str = "") -> SubAgentResult:
        """Ejecuta un sub-agente individual with Active Tracking (Begin/Active pattern)."""
        provider = spec.provider if spec.provider in CHILD_PROVIDERS else "spawn"
        if spec.provider not in CHILD_PROVIDERS:
            logger.warning(f"provider desconocido '{spec.provider}', fallback a spawn")
        mode = spec.mode if spec.mode in CHILD_MODES else "one-shot"
        if spec.mode not in CHILD_MODES:
            logger.warning(f"mode desconocido '{spec.mode}', fallback a one-shot")
        # child_id estable; si el mismo hijo corre concurrente, sufijo anti-colisión.
        child_id = stable_child_id(spec.task, parent_id, depth, provider, mode)
        if child_id in self._active_agents:
            child_id = f"{child_id}-{uuid.uuid4().hex[:4]}"
        agent_id = child_id
        if spec.descriptor:
            # descriptor es log-only: se registra, NUNCA se ejecuta.
            logger.info(f"Sub-agent descriptor (log-only): {spec.descriptor[:200]}")
        if child_id in self._interrupted:
            now_cancelled = datetime.now().isoformat()
            rec = SubAgentResult(agent_id=agent_id, task=spec.task, status=SubAgentStatus.CANCELLED,
                                child_id=child_id, provider=provider, mode=mode, depth=depth,
                                parent_id=parent_id, completed_at=now_cancelled,
                                progress="Interrupted before start")
            self._children[child_id] = rec
            return rec
        self._stats["total_spawned"] += 1

        now = datetime.now().isoformat()
        result = SubAgentResult(
            agent_id=agent_id,
            task=spec.task,
            status=SubAgentStatus.BEGIN,  # Begin state
            begin_time=now,
            parent_id=parent_id,
            progress="Initializing...",
            child_id=child_id,
            provider=provider,
            mode=mode,
            depth=depth,
            child_session_id=self._child_session_id(child_id),
        )
        # Scope estrechado (nunca ensanchado) + sesión hija aislada.
        self._child_scopes[child_id] = self._effective_scope(spec)
        self._ensure_child_session(result.child_session_id)
        self._children[child_id] = result
        self._inbox.setdefault(child_id, [])
        self._active_agents[agent_id] = result
        self._notify_status_change(agent_id, SubAgentStatus.BEGIN, "Created and queued")

        async with self._semaphore:
            # Transition to Active
            result.status = SubAgentStatus.ACTIVE
            result.active_time = datetime.now().isoformat()
            result.progress="Executing task..."
            self._notify_status_change(agent_id, SubAgentStatus.ACTIVE, "Actively working")

            result.started_at = datetime.now().isoformat()
            start = time.time()

            logger.info(f"Sub-agent started: {agent_id} (gem: {spec.gem}, depth: {depth})")
            logger.info(f"  Task: {spec.task[:100]}")

            try:
                # Ejecutar con timeout. `allowed_tools` es opcional: se pasa
                # solo si el executor lo declara (compatibilidad con executors
                # viejos de 3 argumentos).
                call_kwargs = {"gem": spec.gem, "context": spec.context}
                try:
                    params = inspect.signature(self.executor).parameters
                    if "allowed_tools" in params or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                    ):
                        call_kwargs["allowed_tools"] = tuple(spec.allowed_tools or ())
                except (TypeError, ValueError):
                    pass
                if inspect.iscoroutinefunction(self.executor):
                    task_result = await asyncio.wait_for(
                        self.executor(spec.task, **call_kwargs),
                        timeout=spec.timeout_seconds,
                    )
                else:
                    task_result = self.executor(spec.task, **call_kwargs)

                result.status = SubAgentStatus.COMPLETED
                result.result = task_result
                result.progress = "Completed successfully"
                if isinstance(task_result, dict):
                    result.tokens_used = task_result.get("tokens_used", 0)

                self._stats["total_completed"] += 1
                self._notify_status_change(agent_id, SubAgentStatus.COMPLETED, "Done")
                logger.info(f"Sub-agent completed: {agent_id}")

            except asyncio.TimeoutError:
                result.status = SubAgentStatus.TIMEOUT
                result.error = f"Timeout after {spec.timeout_seconds}s"
                result.progress = f"Timed out after {spec.timeout_seconds}s"
                self._stats["total_timeout"] += 1
                self._notify_status_change(agent_id, SubAgentStatus.TIMEOUT, result.progress)
                logger.warning(f"Sub-agent timeout: {agent_id}")

            except asyncio.CancelledError:
                result.status = SubAgentStatus.CANCELLED
                result.progress = "Cancelled by user"
                self._stats["total_cancelled"] += 1
                self._notify_status_change(agent_id, SubAgentStatus.CANCELLED, "Cancelled")
                logger.info(f"Sub-agent cancelled: {agent_id}")

            except Exception as e:
                result.status = SubAgentStatus.FAILED
                result.error = str(e)
                result.progress = f"Failed: {str(e)[:100]}"
                self._stats["total_failed"] += 1
                self._notify_status_change(agent_id, SubAgentStatus.FAILED, result.progress)
                logger.error(f"Sub-agent failed: {agent_id} - {e}")

                # Reintentar si esta configurado
                if spec.max_retries > 0:
                    logger.info(f"Retrying sub-agent {agent_id} ({spec.max_retries} retries left)")
                    spec.max_retries -= 1
                    await asyncio.sleep(1)
                    return await self._run_subagent(spec, depth, parent_id)

            finally:
                result.completed_at = datetime.now().isoformat()
                result.duration_ms = (time.time() - start) * 1000
                self._stats["total_duration_ms"] += result.duration_ms
                self._active_agents.pop(agent_id, None)
                if mode == "one-shot":
                    # fire-and-forget: no se conservan mensajes post-mortem.
                    self._inbox[child_id] = []
                    result.inbox = []

        return result

    def cancel_agent(self, agent_id: str) -> bool:
        """Cancela un sub-agente activo"""
        if agent_id in self._active_agents:
            self._active_agents[agent_id].status = SubAgentStatus.CANCELLED
            self._stats["total_cancelled"] += 1
            return True
        return False

    def cancel_all(self):
        """Cancela todos los sub-agentes activos"""
        for agent_id in list(self._active_agents.keys()):
            self.cancel_agent(agent_id)

    def get_active_agents(self) -> Dict[str, SubAgentResult]:
        """Obtiene sub-agentes activos"""
        return dict(self._active_agents)

    def get_stats(self) -> Dict:
        return {
            **self._stats,
            "active_agents": len(self._active_agents),
            "max_depth": self.max_depth,
            "max_concurrent": self.max_concurrent,
            "avg_duration_ms": (
                self._stats["total_duration_ms"] / max(1, self._stats["total_completed"] + self._stats["total_failed"])
            ),
        }
