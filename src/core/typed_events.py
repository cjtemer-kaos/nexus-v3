"""
Typed Event Bus — parallel event system (aden-hive pattern)

NEW system alongside existing event_bus.py. Provides strongly-typed events
with 40+ concrete event classes organized by category, dedup, history,
stats, and async publish/subscribe via asyncio.

Singleton: ``typed_event_bus`` module-level instance.
"""

import asyncio
import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base event
# ---------------------------------------------------------------------------

@dataclass
class TypedEvent:
    """Base dataclass for all typed events."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    source: str = ""
    event_type: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: int = 3  # 1=critical, 5=low

    def __post_init__(self):
        if not self.event_type:
            # Derive event_type from class name (lowercase, dots for hierarchy)
            self.event_type = type(self).__name__


# ---------------------------------------------------------------------------
# Handler type
# ---------------------------------------------------------------------------

AsyncHandler = Callable[[TypedEvent], Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# Category: AGENT
# ---------------------------------------------------------------------------

@dataclass
class AgentSpawned(TypedEvent):
    event_type: str = "agent.spawned"

@dataclass
class AgentCompleted(TypedEvent):
    event_type: str = "agent.completed"

@dataclass
class AgentFailed(TypedEvent):
    event_type: str = "agent.failed"

@dataclass
class AgentHandoff(TypedEvent):
    event_type: str = "agent.handoff"

@dataclass
class AgentStatusChanged(TypedEvent):
    event_type: str = "agent.status_changed"


# ---------------------------------------------------------------------------
# Category: CHAT
# ---------------------------------------------------------------------------

@dataclass
class ChatMessage(TypedEvent):
    event_type: str = "chat.message"

@dataclass
class ChatStreamingStart(TypedEvent):
    event_type: str = "chat.streaming_start"

@dataclass
class ChatStreamingEnd(TypedEvent):
    event_type: str = "chat.streaming_end"

@dataclass
class ChatError(TypedEvent):
    event_type: str = "chat.error"


# ---------------------------------------------------------------------------
# Category: TOOL
# ---------------------------------------------------------------------------

@dataclass
class ToolCallStart(TypedEvent):
    event_type: str = "tool.call_start"

@dataclass
class ToolCallEnd(TypedEvent):
    event_type: str = "tool.call_end"

@dataclass
class ToolCallError(TypedEvent):
    event_type: str = "tool.call_error"

@dataclass
class ToolCallRetry(TypedEvent):
    event_type: str = "tool.call_retry"


# ---------------------------------------------------------------------------
# Category: MEMORY
# ---------------------------------------------------------------------------

@dataclass
class MemoryWritten(TypedEvent):
    event_type: str = "memory.written"

@dataclass
class MemoryRecalled(TypedEvent):
    event_type: str = "memory.recalled"

@dataclass
class MemoryDeduplicated(TypedEvent):
    event_type: str = "memory.deduplicated"

@dataclass
class EpisodeCreated(TypedEvent):
    event_type: str = "memory.episode_created"

@dataclass
class KnowledgeInjected(TypedEvent):
    event_type: str = "memory.knowledge_injected"


# ---------------------------------------------------------------------------
# Category: TASK
# ---------------------------------------------------------------------------

@dataclass
class TaskCreated(TypedEvent):
    event_type: str = "task.created"

@dataclass
class TaskStarted(TypedEvent):
    event_type: str = "task.started"

@dataclass
class TaskCompleted(TypedEvent):
    event_type: str = "task.completed"

@dataclass
class TaskFailed(TypedEvent):
    event_type: str = "task.failed"

@dataclass
class TaskRetried(TypedEvent):
    event_type: str = "task.retried"

@dataclass
class DAGDecomposed(TypedEvent):
    event_type: str = "task.dag_decomposed"


# ---------------------------------------------------------------------------
# Category: SYSTEM
# ---------------------------------------------------------------------------

@dataclass
class SystemStartup(TypedEvent):
    event_type: str = "system.startup"

@dataclass
class SystemShutdown(TypedEvent):
    event_type: str = "system.shutdown"

@dataclass
class HealthCheck(TypedEvent):
    event_type: str = "system.health_check"

@dataclass
class ConfigChanged(TypedEvent):
    event_type: str = "system.config_changed"

@dataclass
class ModelChanged(TypedEvent):
    event_type: str = "system.model_changed"

@dataclass
class ProviderSwitched(TypedEvent):
    event_type: str = "system.provider_switched"


# ---------------------------------------------------------------------------
# Category: GEMA
# ---------------------------------------------------------------------------

@dataclass
class GemaLoaded(TypedEvent):
    event_type: str = "gema.loaded"

@dataclass
class GemaExecuted(TypedEvent):
    event_type: str = "gema.executed"

@dataclass
class GemaError(TypedEvent):
    event_type: str = "gema.error"

@dataclass
class GemaRouting(TypedEvent):
    event_type: str = "gema.routing"


# ---------------------------------------------------------------------------
# Category: BROWSER
# ---------------------------------------------------------------------------

@dataclass
class BrowserOpened(TypedEvent):
    event_type: str = "browser.opened"

@dataclass
class BrowserNavigated(TypedEvent):
    event_type: str = "browser.navigated"

@dataclass
class BrowserScreenshot(TypedEvent):
    event_type: str = "browser.screenshot"

@dataclass
class BrowserClosed(TypedEvent):
    event_type: str = "browser.closed"


# ---------------------------------------------------------------------------
# Category: VOICE
# ---------------------------------------------------------------------------

@dataclass
class VoiceListening(TypedEvent):
    event_type: str = "voice.listening"

@dataclass
class VoiceTranscribed(TypedEvent):
    event_type: str = "voice.transcribed"

@dataclass
class VoiceSpeaking(TypedEvent):
    event_type: str = "voice.speaking"

@dataclass
class VoiceError(TypedEvent):
    event_type: str = "voice.error"


# ---------------------------------------------------------------------------
# Category: SECURITY
# ---------------------------------------------------------------------------

@dataclass
class CapabilityGranted(TypedEvent):
    event_type: str = "security.capability_granted"

@dataclass
class CapabilityRevoked(TypedEvent):
    event_type: str = "security.capability_revoked"

@dataclass
class TaintDetected(TypedEvent):
    event_type: str = "security.taint_detected"

@dataclass
class PermissionDenied(TypedEvent):
    event_type: str = "security.permission_denied"


# ---------------------------------------------------------------------------
# Registry: event_type string → class
# ---------------------------------------------------------------------------

EVENT_REGISTRY: Dict[str, type] = {}


def _register_events():
    """Auto-register all TypedEvent subclasses by their event_type."""
    for subclass in TypedEvent.__subclasses__():
        if subclass is TypedEvent:
            continue
        # Create a default instance to read event_type
        try:
            instance = subclass()
            EVENT_REGISTRY[instance.event_type] = subclass
        except TypeError:
            logger.warning("Could not auto-register %s (needs required args)", subclass.__name__)


_register_events()


# ---------------------------------------------------------------------------
# TypedEventBus
# ---------------------------------------------------------------------------

class TypedEventBus:
    """
    Async event bus with typed events, dedup, history, and stats.

    Usage::

        bus = TypedEventBus()
        await bus.subscribe("agent.spawned", my_handler)
        await bus.publish(AgentSpawned(source="orchestrator"))
    """

    def __init__(self, max_seen: int = 128, max_history: int = 500):
        self._subscribers: Dict[str, List[AsyncHandler]] = defaultdict(list)
        self._wildcard_subscribers: List[AsyncHandler] = []
        self._seen_ids: deque = deque(maxlen=max_seen)
        self._seen_set: Set[str] = set()
        self._max_seen = max_seen
        self._history: deque = deque(maxlen=max_history)
        self._stats: Dict[str, int] = defaultdict(int)

    # -- subscribe / unsubscribe -------------------------------------------

    async def subscribe(self, event_type: str, handler: AsyncHandler) -> None:
        """
        Register *handler* for *event_type*.

        Pass ``"*"`` as event_type to subscribe to all events (wildcard).
        """
        if event_type == "*":
            self._wildcard_subscribers.append(handler)
            logger.debug("Wildcard subscriber added: %s", handler)
        else:
            self._subscribers[event_type].append(handler)
            logger.debug("Subscribed %s to %s", handler, event_type)

    async def unsubscribe(self, event_type: str, handler: AsyncHandler) -> bool:
        """Remove *handler*. Returns True if it was actually removed."""
        if event_type == "*":
            try:
                self._wildcard_subscribers.remove(handler)
                return True
            except ValueError:
                return False
        try:
            self._subscribers[event_type].remove(handler)
            return True
        except ValueError:
            return False

    # -- publish -----------------------------------------------------------

    async def publish(self, event: TypedEvent) -> bool:
        """
        Publish *event* to matching subscribers.

        Returns False if the event was dropped (dedup).
        Handlers are invoked concurrently; errors are logged but do not
        prevent other handlers from running.
        """
        # Dedup
        if event.id in self._seen_set:
            logger.debug("Dropping duplicate event %s (%s)", event.id, event.event_type)
            return False

        self._seen_set.add(event.id)
        self._seen_ids.append(event.id)

        # Evict from set when deque rotates
        if len(self._seen_ids) >= self._max_seen:
            old_id = self._seen_ids[0]
            if old_id not in list(self._seen_ids)[1:]:
                self._seen_set.discard(old_id)

        # Record history & stats
        self._history.append(event)
        self._stats[event.event_type] += 1
        self._stats["__total__"] += 1

        # Gather handlers
        handlers = list(self._subscribers.get(event.event_type, []))
        handlers.extend(self._wildcard_subscribers)

        if not handlers:
            return True

        # Dispatch concurrently, catching per-handler errors
        tasks = []
        for h in handlers:
            tasks.append(self._safe_call(h, event))
        await asyncio.gather(*tasks)
        return True

    @staticmethod
    async def _safe_call(handler: AsyncHandler, event: TypedEvent) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception(
                "Handler %s raised on %s", handler, event.event_type
            )

    # -- history -----------------------------------------------------------

    def get_history(self, event_type: Optional[str] = None, limit: int = 50) -> List[TypedEvent]:
        """
        Return recent events, optionally filtered by *event_type*.
        Most recent last (chronological order).
        """
        events = list(self._history)
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return events[-limit:]

    # -- stats -------------------------------------------------------------

    def get_stats(self) -> Dict[str, int]:
        """Return event counts by type, plus ``__total__``."""
        return dict(self._stats)

    # -- reset / lifecycle -------------------------------------------------

    def reset(self) -> None:
        """Clear all state (useful for tests)."""
        self._subscribers.clear()
        self._wildcard_subscribers.clear()
        self._seen_ids.clear()
        self._seen_set.clear()
        self._history.clear()
        self._stats.clear()

    def shutdown(self) -> None:
        """Clear everything (for clean shutdown)."""
        self.reset()
        logger.info("TypedEventBus shut down")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

typed_event_bus = TypedEventBus()
