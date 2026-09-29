"""
Actor Model — Gemas como Actors con mailbox, ciclo de vida y supervisión.

Patrón absorbido de Lethe + Erlang/OTP:
  - Cada gema es un actor con buzón de mensajes asíncrono
  - Procesamiento secuencial de mensajes (un actor = una tarea a la vez)
  - Árbol de supervisión: padre reinicia hijo si falla
  - ActorSystem: registro, enrutamiento, health checks
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
import json
import re

logger = logging.getLogger(__name__)


class MessageIntent(Enum):
    """Intención semántica de un mensaje entre actores."""
    TASK = "task"           # Ejecutar una tarea con herramientas
    QUERY = "query"         # Preguntar información o estado
    COMMAND = "command"     # Orden directa (stop, restart, etc.)
    NOTIFY = "notify"       # Notificación/evento (fire-and-forget)
    SUPERVISE = "supervise"  # Control de supervisión
    BROADCAST = "broadcast"  # Mensaje a todos
    ROUTE = "route"         # Mensaje que necesita ser ruteado


INTENT_KEYWORDS: dict[MessageIntent, list[str]] = {
    MessageIntent.TASK: ["implementa", "crea", "genera", "refactoriza", "analiza", "revisa", "busca",
                         "ejecuta", "compila", "despliega", "convierte", "transforma"],
    MessageIntent.QUERY: ["qué es", "cómo funciona", "explica", "dime", "muestra", "lista",
                          "status", "estado", "health", "qué hay"],
    MessageIntent.COMMAND: ["stop", "detente", "reinicia", "restart", "cancel", "cancela", "pause", "pausa"],
    MessageIntent.NOTIFY: ["notifica", "avisa", "informa", "terminó", "completado"],
    MessageIntent.SUPERVISE: ["supervisa", "monitorea", "revisa estado"],
    MessageIntent.BROADCAST: ["anuncia", "broadcast", "todos", "all"],
}

GEMA_KEYWORDS: dict[str, list[str]] = {
    "code": ["código", "programar", "python", "javascript", "typescript", "bug", "refactor", "implementar"],
    "architect": ["arquitectura", "diseño", "infraestructura", "escalabilidad", "patrones"],
    "scholar": ["investigar", "aprender", "documentación", "artículo", "research", "estudio"],
    "creative": ["creativo", "escribir", "contenido", "narrativa", "blog", "copy"],
    "debugger": ["debug", "error", "fallo", "crash", "excepción", "traceback"],
    "security": ["seguridad", "vulnerabilidad", "auditoría", "cifrado", "oauth"],
    "devops": ["despliegue", "docker", "kubernetes", "ci/cd", "infra"],
    "tester": ["test", "prueba", "qa", "cobertura", "assert"],
    "analyst": ["analizar", "datos", "métrica", "dashboard", "reporte"],
    "optimizer": ["optimizar", "rendimiento", "lento", "cuello de botella", "memoria"],
    "director": ["orquestar", "planificar", "coordinar", "organizar", "estrategia"],
    "sage": ["recordar", "memoria", "aprendizaje", "conocimiento"],
    "ayuda": ["ayuda", "asistencia", "soporte", "tutorial", "guiar"],
}


def classify_intent(content: str) -> MessageIntent:
    """Clasifica la intención de un mensaje por keywords + longitud."""
    lower = content.lower()
    for intent, keywords in INTENT_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return intent
    if len(content) < 80 and any(c in content for c in "¿?¡!"):
        return MessageIntent.QUERY
    if len(content) > 40:
        return MessageIntent.TASK
    return MessageIntent.QUERY


def route_by_content(content: str, actors: dict[str, Actor]) -> str | None:
    """Encuentra el actor más adecuado según el contenido del mensaje."""
    lower = content.lower()
    words = set(re.findall(r'\w+', lower))
    scores: dict[str, int] = {}
    for actor_id, actor in actors.items():
        gema_name = actor.name.split("::")[-1].lower() if "::" in actor.name else actor.name.lower()
        keywords = GEMA_KEYWORDS.get(gema_name, [gema_name])
        score = 0
        for kw in keywords:
            kw_lower = kw.lower()
            if len(kw_lower) > 3 and kw_lower in lower:
                score += 1
            elif any(w.startswith(kw_lower[:4]) or kw_lower.startswith(w[:4]) for w in words):
                score += 1
        if score > 0:
            scores[actor_id] = score
    if not scores:
        return None
    return max(scores, key=scores.get)


class ActorState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    CRASHED = "crashed"
    RESTARTING = "restarting"


class SupervisorStrategy(Enum):
    ONE_FOR_ONE = "one_for_one"
    ONE_FOR_ALL = "one_for_all"
    REST_FOR_ONE = "rest_for_one"


@dataclass
class ActorMessage:
    """Mensaje en el buzón de un actor."""
    id: str = ""
    sender: str = ""
    msg_type: str = "task"
    content: str = ""
    metadata: dict = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:12]
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class ActorResult:
    """Resultado del procesamiento de un mensaje."""
    success: bool = False
    content: str = ""
    error: str = ""
    duration_s: float = 0.0
    metadata: dict = field(default_factory=dict)


class Actor(ABC):
    """Actor base. Cada instancia tiene su propio buzón y ciclo de vida."""

    name: str = "actor"
    max_restarts: int = 3
    restart_window_s: float = 60.0
    intents: list[MessageIntent] = [MessageIntent.TASK, MessageIntent.QUERY]

    def __init__(self, actor_id: str = ""):
        self.actor_id = actor_id or f"{self.name}_{uuid.uuid4().hex[:6]}"
        self.state = ActorState.IDLE
        self._mailbox: asyncio.Queue[ActorMessage] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._parent: Actor | None = None
        self._children: dict[str, Actor] = {}
        self._restart_history: list[float] = []
        self._started_at: float = 0.0
        self._messages_processed: int = 0
        self._errors: int = 0
        self._last_error: str = ""
        self.focus: TaskFocusState | None = None
        self._stop_hook: StopHook | None = None

    def set_parent(self, parent: Actor) -> None:
        self._parent = parent

    def add_child(self, child: Actor) -> None:
        self._children[child.actor_id] = child
        child.set_parent(self)

    def remove_child(self, actor_id: str) -> None:
        self._children.pop(actor_id, None)

    def get_child(self, actor_id: str) -> Actor | None:
        return self._children.get(actor_id)

    def tell(self, msg: ActorMessage | str, msg_type: str = "task", sender: str = "") -> None:
        """Envía un mensaje al buzón (fire-and-forget)."""
        if isinstance(msg, str):
            msg = ActorMessage(content=msg, msg_type=msg_type, sender=sender)
        self._mailbox.put_nowait(msg)

    async def ask(self, content: str, msg_type: str = "task", timeout: float = 120.0) -> ActorResult:
        """Envía un mensaje y espera respuesta."""
        response_queue: asyncio.Queue[ActorResult] = asyncio.Queue()
        msg = ActorMessage(content=content, msg_type=msg_type, metadata={"__response_queue": response_queue})
        self._mailbox.put_nowait(msg)
        try:
            return await asyncio.wait_for(response_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return ActorResult(success=False, content="", error="Actor response timeout")

    async def spawn(self) -> None:
        """Inicia el procesamiento del buzón."""
        if self._task and not self._task.done():
            return
        self.state = ActorState.RUNNING
        self._started_at = time.time()
        self._task = asyncio.create_task(self._run_loop(), name=f"actor:{self.actor_id}")
        logger.info("Actor spawned: %s", self.actor_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Detiene el actor."""
        self.state = ActorState.STOPPED
        for child in list(self._children.values()):
            await child.stop(timeout)
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info("Actor stopped: %s", self.actor_id)

    async def _run_loop(self) -> None:
        """Loop principal: procesa mensajes del buzón secuencialmente."""
        try:
            await self.on_start()
            while self.state != ActorState.STOPPED:
                try:
                    msg = self._mailbox.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.5)
                    continue

                if self.state == ActorState.STOPPED:
                    break

                result = await self._handle_with_supervision(msg)

                response_q = msg.metadata.get("__response_queue")
                if response_q:
                    response_q.put_nowait(result)

                self._messages_processed += 1
        except asyncio.CancelledError:
            pass
        finally:
            await self.on_stop()

    async def _handle_with_supervision(self, msg: ActorMessage) -> ActorResult:
        """Procesa un mensaje con supervisión: reintenta si falla."""
        try:
            result = await self.handle_message(msg)
            if not result.success:
                self._errors += 1
                self._last_error = result.error
            return result
        except Exception as e:
            self._errors += 1
            self._last_error = str(e)
            logger.error("Actor %s error: %s", self.actor_id, e)
            self.state = ActorState.CRASHED
            await self._try_restart()
            return ActorResult(success=False, content="", error=str(e))

    async def _try_restart(self) -> bool:
        """Intenta reiniciar el actor si está dentro del límite."""
        now = time.time()
        self._restart_history = [t for t in self._restart_history if now - t < self.restart_window_s]
        if len(self._restart_history) >= self.max_restarts:
            logger.critical("Actor %s: max restarts exceeded (%d in %.0fs)",
                            self.actor_id, self.max_restarts, self.restart_window_s)
            return False
        self._restart_history.append(now)
        self.state = ActorState.RESTARTING
        logger.warning("Actor %s: restarting (%d/%d)", self.actor_id, len(self._restart_history), self.max_restarts)
        await asyncio.sleep(1)
        self.state = ActorState.RUNNING
        return True

    @abstractmethod
    async def handle_message(self, msg: ActorMessage) -> ActorResult:
        ...

    async def on_start(self) -> None:
        """Hook de inicialización."""
        pass

    async def on_stop(self) -> None:
        """Hook de limpieza."""
        pass

    def status(self) -> dict:
        s = {
            "actor_id": self.actor_id,
            "name": self.name,
            "state": self.state.value,
            "messages_processed": self._messages_processed,
            "errors": self._errors,
            "last_error": self._last_error,
            "children": list(self._children.keys()),
            "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0,
        }
        if self.focus:
            s["focus"] = self.focus.summary()
        return s


class ActorSystem:
    """Sistema de actores: registro, supervisión, enrutamiento."""

    def __init__(self):
        self._actors: dict[str, Actor] = {}
        self._stats: dict[str, int] = {}

    def register(self, actor: Actor, parent: Actor | None = None) -> Actor:
        self._actors[actor.actor_id] = actor
        if parent:
            parent.add_child(actor)
        logger.info("ActorSystem: registered %s", actor.actor_id)
        return actor

    def unregister(self, actor_id: str) -> None:
        actor = self._actors.pop(actor_id, None)
        if actor:
            logger.info("ActorSystem: unregistered %s", actor_id)

    async def start_all(self) -> None:
        """Spawn todos los actores registrados."""
        for actor in self._actors.values():
            await actor.spawn()
        logger.info("ActorSystem: all %d actors started", len(self._actors))

    async def start_by_name(self, name: str) -> None:
        for actor in self._actors.values():
            if actor.name == name:
                await actor.spawn()

    def get(self, actor_id: str) -> Actor | None:
        return self._actors.get(actor_id)

    def find_by_name(self, name: str) -> list[Actor]:
        return [a for a in self._actors.values() if a.name == name]

    async def tell(self, actor_id: str, msg: ActorMessage | str, msg_type: str = "task", sender: str = "") -> None:
        actor = self._actors.get(actor_id)
        if actor:
            actor.tell(msg, msg_type=msg_type, sender=sender)
        else:
            logger.warning("ActorSystem: %s not found for tell", actor_id)

    async def ask(self, actor_id: str, content: str, msg_type: str = "task", timeout: float = 120.0) -> ActorResult:
        actor = self._actors.get(actor_id)
        if actor:
            return await actor.ask(content, msg_type=msg_type, timeout=timeout)
        return ActorResult(success=False, content="", error=f"Actor {actor_id} not found")

    def route(self, content: str, intent: MessageIntent | None = None) -> str | None:
        """Enruta contenido al actor más adecuado por semántica + intención."""
        if intent and intent == MessageIntent.SUPERVISE:
            return self._find_one_by_name("supervisor")
        if intent and intent == MessageIntent.COMMAND:
            return self._find_one_by_name("supervisor")
        return route_by_content(content, self._actors)

    def _find_one_by_name(self, name: str) -> str | None:
        for aid, a in self._actors.items():
            if a.name == name:
                return aid
        return None

    async def route_and_tell(self, content: str, msg_type: str = "task", sender: str = "") -> None:
        """Enruta y envía fire-and-forget al mejor actor."""
        target = self.route(content)
        if target:
            await self.tell(target, content, msg_type=msg_type, sender=sender)
        else:
            logger.warning("ActorSystem: no target for route '%s'", content[:60])

    async def route_and_ask(self, content: str, msg_type: str = "task", timeout: float = 120.0) -> ActorResult:
        """Enruta y espera respuesta del mejor actor."""
        target = self.route(content)
        if target:
            return await self.ask(target, content, msg_type=msg_type, timeout=timeout)
        return ActorResult(success=False, content="", error=f"No target found for: {content[:60]}")

    async def broadcast(self, content: str, msg_type: str = "task", name_filter: str = "") -> dict[str, ActorResult]:
        targets = [a for a in self._actors.values() if not name_filter or a.name == name_filter]
        results = {}
        for actor in targets:
            results[actor.actor_id] = await actor.ask(content, msg_type=msg_type)
        return results

    async def stop_all(self, timeout: float = 10.0) -> None:
        for actor in list(self._actors.values()):
            await actor.stop(timeout)
        self._actors.clear()
        logger.info("ActorSystem: all actors stopped")

    def status(self) -> dict:
        return {
            "total_actors": len(self._actors),
            "actors": {aid: a.status() for aid, a in self._actors.items()},
            "routing_stats": dict(self._stats),
        }

    def snapshots(self) -> list[ActorSnapshot]:
        return [a.snapshot() if hasattr(a, "snapshot") and callable(a.snapshot)
                else ActorSnapshot(actor_id=a.actor_id, name=a.name, state=a.state.value,
                                   messages_processed=a._messages_processed, errors=a._errors)
                for a in self._actors.values()]


class GemaActor(Actor):
    """Actor que envuelve una gema con su provider + AgentRunner."""

    def __init__(self, name: str, model: str, provider_registry: Any,
                 tool_executor: Callable | None = None, get_tool_schemas: Callable | None = None,
                 actor_id: str = ""):
        super().__init__(actor_id=actor_id)
        self.name = name
        self.model = model
        self._provider_registry = provider_registry
        self._tool_executor = tool_executor
        self._get_tool_schemas = get_tool_schemas
        self._runner_ref: dict = {}

    def _get_runner(self):
        from src.core.agent_runner import AgentRunner, AgentRunSpec
        from src.core.provider_base import LLMMessage
        provider = self._provider_registry.get_or_fallback(self.name)
        return provider, AgentRunner, AgentRunSpec, LLMMessage

    async def handle_message(self, msg: ActorMessage) -> ActorResult:
        start = time.time()
        provider, AgentRunnerCls, AgentRunSpecCls, LLMMessageCls = self._get_runner()

        tool_schemas = self._get_tool_schemas() if self._get_tool_schemas else []
        spec = AgentRunSpecCls(
            messages=[LLMMessageCls(role="user", content=msg.content)],
            tools_definitions=tool_schemas,
            max_iterations=5,
        )
        runner = AgentRunnerCls(provider, tool_executor=self._tool_executor)
        result = await runner.run(spec)

        if result.stop_reason == "error":
            return ActorResult(success=False, content="", error=result.error or "Unknown error",
                               duration_s=time.time() - start)
        return ActorResult(success=True, content=result.content or "",
                           duration_s=time.time() - start,
                           metadata={"usage": result.usage, "tools_used": result.tools_used})

    def status(self) -> dict:
        s = super().status()
        s["model"] = self.model
        s["provider"] = self.name
        return s


class BackgroundCognition(Actor):
    """DMN (Default Mode Network) — cognición de fondo.

    Fusiona BackgroundReviewDaemon + Scheduler en un actor:
    - Revisa conversaciones recientes periódicamente
    - Consolida memoria
    - Encuentra patrones
    - Ejecuta tareas programadas cuando el sistema está idle
    """

    def __init__(self, review_daemon: Any = None, worker_manager: Any = None,
                 memory_consolidator: Any = None, brain_fn: Callable | None = None,
                 interval_s: float = 60.0, actor_id: str = ""):
        super().__init__(actor_id=actor_id)
        self.name = "background_cognition"
        self._review_daemon = review_daemon
        self._worker_manager = worker_manager
        self._memory_consolidator = memory_consolidator
        self._brain_fn = brain_fn
        self._interval_s = interval_s
        self._last_review: float = 0.0
        self._cycles: int = 0

    async def on_start(self) -> None:
        self._loop_task = asyncio.create_task(self._cognition_loop(), name=f"dmn:{self.actor_id}")

    async def on_stop(self) -> None:
        if hasattr(self, "_loop_task") and self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await asyncio.wait_for(self._loop_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    async def _cognition_loop(self) -> None:
        """Loop de cognición de fondo."""
        while self.state != ActorState.STOPPED:
            try:
                await asyncio.sleep(self._interval_s)
                await self._cognition_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("DMN cycle error: %s", e)

    async def _cognition_cycle(self) -> None:
        """Un ciclo de cognición: revisar + consolidar + programar."""
        self._cycles += 1
        self._last_review = time.time()

        # 1. Consolidación de memoria
        if self._memory_consolidator:
            try:
                result = await self._memory_consolidator.consolidate(new_facts=[])
                logger.debug("DMN: memory consolidation -> %s", result)
            except Exception as e:
                logger.warning("DMN: memory consolidation error: %s", e)

        # 2. Revisión de fondo (si hay contenido reciente)
        if self._review_daemon and self._review_daemon._reviews_run < self._cycles:
            logger.debug("DMN: background review cycle %d", self._cycles)

        # 3. Tareas programadas
        if self._worker_manager:
            try:
                await self._worker_manager.process_scheduled()
            except Exception as e:
                logger.warning("DMN: scheduler error: %s", e)

        # 4. Auto-aprendizaje si hay brain_fn
        if self._brain_fn:
            try:
                await self._brain_fn("DMN", f"Background cognition cycle #{self._cycles}")
            except Exception as e:
                logger.debug("DMN: brain recall ping: %s", e)

    async def handle_message(self, msg: ActorMessage) -> ActorResult:
        if msg.msg_type == "dmn:trigger":
            await self._cognition_cycle()
            return ActorResult(success=True, content=f"DMN cycle #{self._cycles} completed",
                               metadata={"cycles": self._cycles})
        if msg.msg_type == "dmn:status":
            return ActorResult(success=True, content=json.dumps(self.status()),
                               metadata={"cycles": self._cycles})
        return ActorResult(success=False, content="", error=f"Unknown msg_type: {msg.msg_type}")

    def status(self) -> dict:
        s = super().status()
        s["cycles"] = self._cycles
        s["interval_s"] = self._interval_s
        s["last_review"] = self._last_review
        return s


# ── Sprint 3: TaskFocusState + RalphLoop + StopHook ──────────────

@dataclass
class TaskFocusState:
    """Self-awareness del progreso de una tarea larga (patrón ohmo)."""
    goal: str = ""
    steps_completed: list[str] = field(default_factory=list)
    current_step: str = ""
    blockers: list[str] = field(default_factory=list)
    tokens_used: int = 0
    start_time: float = 0.0
    last_checkin: float = 0.0
    iteration: int = 0

    def checkin(self, step: str = "", tokens: int = 0) -> dict:
        self.iteration += 1
        self.last_checkin = time.time()
        self.tokens_used += tokens
        if step:
            self.steps_completed.append(step)
        return self.summary()

    def summary(self) -> dict:
        elapsed = round(time.time() - self.start_time, 1) if self.start_time else 0
        return {
            "goal": self.goal[:80],
            "done": len(self.steps_completed),
            "current": self.current_step,
            "blockers": self.blockers,
            "tokens": self.tokens_used,
            "elapsed_s": elapsed,
            "iteration": self.iteration,
        }

    def to_dict(self) -> dict:
        return {
            "goal": self.goal, "steps_completed": self.steps_completed,
            "current_step": self.current_step, "blockers": self.blockers,
            "tokens_used": self.tokens_used, "start_time": self.start_time,
            "last_checkin": self.last_checkin, "iteration": self.iteration,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskFocusState":
        return cls(**{k: d.get(k, v.default if hasattr(v, 'default') else "")
                      for k, v in cls.__dataclass_fields__.items()})


class StopHook:
    """Hook cooperativo para detener una tarea en ejecución (RalphLoop)."""

    def __init__(self):
        self._stopped = False
        self._reason = ""

    def stop(self, reason: str = "") -> None:
        self._stopped = True
        self._reason = reason

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    @property
    def reason(self) -> str:
        return self._reason

    def check(self) -> None:
        """Llamar periódicamente en tareas largas. Lanza si se pidió stop."""
        if self._stopped:
            raise InterruptedError(f"StopHook: {self._reason}")


async def ralph_loop(func: Callable, *args,
                     max_retries: int = 3, base_delay: float = 1.0,
                     stop_hook: StopHook | None = None,
                     on_retry: Callable | None = None,
                     **kwargs) -> Any:
    """RalphLoop — retry con backoff exponencial y StopHook."""
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        if stop_hook and stop_hook.is_stopped:
            raise InterruptedError(stop_hook.reason)
        try:
            return await func(*args, **kwargs)
        except InterruptedError:
            raise
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning("RalphLoop retry %d/%d in %.1fs: %s", attempt + 1, max_retries, delay, e)
                if on_retry:
                    on_retry(attempt, e, delay)
                await asyncio.sleep(delay)
    raise last_error or RuntimeError("RalphLoop exhausted")


# ── Sprint 4: ModelCatalog + IntelligentRouter ───────────────────

@dataclass
class ModelInfo:
    """Metadata de un modelo en el catálogo."""
    name: str
    provider: str = "ollama"
    context_window: int = 4096
    cost_per_1k: float = 0.0
    speed_tps: float = 20.0
    capabilities: list[str] = field(default_factory=lambda: ["chat"])
    quality_rating: float = 5.0  # 1-10

MODEL_CATALOG: dict[str, ModelInfo] = {
    "nexus-coder": ModelInfo("nexus-coder", "ollama", 8192, 0, 25, ["chat", "code", "tools"], 7),
    "qwen2.5:0.5b": ModelInfo("qwen2.5:0.5b", "ollama", 32768, 0, 80, ["chat"], 3),
    "qwen2.5-coder:7b": ModelInfo("qwen3.5-9b", "llama", 131072, 0, 35, ["chat", "code", "creative"], 8),
    "deepseek-r1:8b": ModelInfo("deepseek-r1:8b", "ollama", 131072, 0, 20, ["chat", "reasoning", "research"], 9),
    "nemotron-3-nano:4b": ModelInfo("nemotron-3-nano:4b", "ollama", 8192, 0, 50, ["chat", "analysis"], 5),
    "qwen2.5vl:7b": ModelInfo("qwen2.5vl:7b", "ollama", 32768, 0, 25, ["chat", "vision"], 6),
    "nomic-embed-text": ModelInfo("nomic-embed-text", "ollama", 8192, 0, 100, ["embedding"], 8),
}


def select_model(goal: str, required_context: int = 0,
                 prefer_speed: bool = False, prefer_quality: bool = False) -> str:
    """IntelligentRouter: elige el mejor modelo según la tarea."""
    lower = goal.lower()
    words = set(re.findall(r'\w+', lower))
    candidates = list(MODEL_CATALOG.values())

    def matches_keywords(keywords: list[str]) -> bool:
        for kw in keywords:
            kw_lower = kw.lower()
            if len(kw_lower) > 3 and kw_lower in lower:
                return True
            if any(w.startswith(kw_lower[:4]) or kw_lower.startswith(w[:4]) for w in words):
                return True
        return False

    if required_context > 0:
        candidates = [m for m in candidates if m.context_window >= required_context]

    # Priority: vision > code > reasoning > creative (most specific first)
    if matches_keywords(["imagen", "captura", "screenshot", "vision"]):
        candidates = [m for m in candidates if "vision" in m.capabilities]
    elif matches_keywords(["código", "programar", "implementar", "bug"]):
        candidates = [m for m in candidates if "code" in m.capabilities]
    elif matches_keywords(["investigar", "analizar", "research"]):
        candidates = [m for m in candidates if "reasoning" in m.capabilities or "research" in m.capabilities]
    elif matches_keywords(["creativo", "escribir", "narrativa"]):
        candidates = [m for m in candidates if "creative" in m.capabilities]

    if not candidates:
        candidates = list(MODEL_CATALOG.values())

    # Exclude embedding-only models for task routing
    candidates = [m for m in candidates if m.name != "nomic-embed-text"]

    if prefer_quality:
        candidates.sort(key=lambda m: m.quality_rating, reverse=True)
    elif prefer_speed:
        candidates.sort(key=lambda m: m.speed_tps, reverse=True)
    else:
        candidates.sort(key=lambda m: (m.quality_rating * 0.7 + min(m.speed_tps / 10, 10) * 0.3), reverse=True)

    return candidates[0].name


class IntelligentRouter(Actor):
    """Router semántico que usa ModelCatalog para elegir modelo + actor."""

    def __init__(self, system: ActorSystem, actor_id: str = ""):
        super().__init__(actor_id=actor_id)
        self.name = "intelligent_router"

    async def handle_message(self, msg: ActorMessage) -> ActorResult:
        intent = classify_intent(msg.content)
        model = select_model(msg.content, prefer_quality=(intent == MessageIntent.TASK))

        gema_target = self._parent.get_child("router-v1") if self._parent else None
        if gema_target:
            logger.info("IntelligentRouter: %s -> model=%s, intent=%s", msg.content[:40], model, intent.value)
            return await gema_target.ask(msg.content, msg_type=msg.msg_type,
                                          timeout=msg.metadata.get("timeout", 120.0))
        target = self._system.route(msg.content, intent)
        if target:
            return await self._system.ask(target, msg.content, msg_type=msg.msg_type,
                                           timeout=msg.metadata.get("timeout", 120.0))
        return ActorResult(success=False, content="", error=f"No route for: {msg.content[:60]}")


# ── Sprint 5: Actor Persistence ──────────────────────────────────

@dataclass
class ActorSnapshot:
    """Snapshot serializable del estado de un actor."""
    actor_id: str
    name: str
    state: str
    messages_processed: int
    errors: int
    focus: dict | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"actor_id": self.actor_id, "name": self.name, "state": self.state,
                "messages_processed": self.messages_processed, "errors": self.errors,
                "focus": self.focus, "metadata": self.metadata}


class PersistableActor(Actor):
    """Actor que puede guardar/restaurar su estado desde un snapshot."""

    persist_attrs: list[str] = ["_messages_processed", "_errors", "_started_at"]

    def snapshot(self) -> ActorSnapshot:
        meta = {attr: getattr(self, attr, None) for attr in self.persist_attrs}
        focus = self.focus.to_dict() if hasattr(self, "focus") and self.focus else None
        return ActorSnapshot(actor_id=self.actor_id, name=self.name,
                             state=self.state.value, messages_processed=self._messages_processed,
                             errors=self._errors, focus=focus, metadata=meta)

    def restore(self, snap: ActorSnapshot) -> None:
        self._messages_processed = snap.messages_processed
        self._errors = snap.errors
        for k, v in snap.metadata.items():
            if hasattr(self, k):
                setattr(self, k, v)
        if snap.focus and hasattr(self, "focus") and self.focus:
            self.focus = TaskFocusState.from_dict(snap.focus)


class RouterActor(Actor):
    """Actor que recibe mensajes sin destino, clasifica intent y re-rutea."""

    intents = [MessageIntent.ROUTE, MessageIntent.TASK, MessageIntent.QUERY]

    def __init__(self, system: ActorSystem, actor_id: str = ""):
        super().__init__(actor_id=actor_id)
        self.name = "router"
        self._system = system

    async def handle_message(self, msg: ActorMessage) -> ActorResult:
        intent = classify_intent(msg.content)
        self._system._stats[intent.value] = self._system._stats.get(intent.value, 0) + 1

        if msg.msg_type == "route" or msg.metadata.get("needs_routing"):
            target = self._system.route(msg.content, intent=intent)
            if target:
                logger.info("RouterActor: %s -> %s (intent=%s)", msg.content[:40], target, intent.value)
                return await self._system.ask(target, msg.content, msg_type=msg.msg_type,
                                               timeout=msg.metadata.get("timeout", 120.0))
            return ActorResult(success=False, content="", error=f"No actor found for: {msg.content[:60]}")
        return ActorResult(success=True, content=f"Routing not needed (intent={intent.value})")


class SupervisorActor(Actor):
    """Actor que supervisa otros actores con estrategia configurable."""

    intents = [MessageIntent.SUPERVISE, MessageIntent.COMMAND]

    def __init__(self, strategy: SupervisorStrategy = SupervisorStrategy.ONE_FOR_ONE,
                 actor_id: str = ""):
        super().__init__(actor_id=actor_id)
        self.name = "supervisor"
        self.strategy = strategy

    async def handle_message(self, msg: ActorMessage) -> ActorResult:
        if msg.msg_type == "supervise:restart":
            target_id = msg.content.strip()
            child = self.get_child(target_id)
            if child:
                logger.info("Supervisor restarting child: %s", target_id)
                await child.stop()
                await child.spawn()
                if self.strategy == SupervisorStrategy.ONE_FOR_ALL:
                    for c in self._children.values():
                        if c.actor_id != target_id:
                            await c.stop()
                            await c.spawn()
                return ActorResult(success=True, content=f"Restarted {target_id}")
            return ActorResult(success=False, content="", error=f"Child {target_id} not found")
        if msg.msg_type == "supervise:status":
            return ActorResult(success=True, content=str({aid: a.status() for aid, a in self._children.items()}))
        return ActorResult(success=False, content="", error=f"Unknown msg_type: {msg.msg_type}")
