"""
Event Bus — wrapper legacy que delega a RealtimeHub

Mantiene la API original (EventBus, EventType, Message, Handoff, MessageQueue)
pero internamente usa RealtimeHub para pub/sub con scopes + dedup + Redis.

Esto permite que server.py, communication.py y runtime.py obtengan
automáticamente scoped subscriptions, dedup y broadcast multi-nodo
sin cambiar una línea de su código.
"""

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


class EventType(Enum):
    # Core
    MESSAGE = "message"
    HANDOFF = "handoff"
    TASK_COMPLETE = "task_complete"
    TASK_FAILED = "task_failed"
    LEARNING = "learning"
    MEMORY_UPDATE = "memory_update"
    ENGINE_STATUS = "engine_status"
    USER_INPUT = "user_input"
    SYSTEM = "system"
    # Worker / daemon (aden-hive parity)
    WORKER_STALLED = "worker_stalled"
    WORKER_RECOVERED = "worker_recovered"
    NODE_STALLED = "node_stalled"
    STREAM_TTFT_EXCEEDED = "stream_ttft_exceeded"
    HEALTH_DEGRADED = "health_degraded"
    HEALTH_RECOVERED = "health_recovered"
    # DMN
    DMN_NOTIFY = "dmn_notify"
    DMN_ROUND_COMPLETE = "dmn_round_complete"
    # Memory / salience
    SALIENCE_TAGGED = "salience_tagged"
    MEMORY_COMPACTED = "memory_compacted"
    # Approval / checkpoint
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    CHECKPOINT_WRITTEN = "checkpoint_written"
    # Provider / tool
    PROVIDER_FALLBACK = "provider_fallback"
    TOOL_BLOCKED = "tool_blocked"
    # Federation / bridge
    BRIDGE_CONNECTED = "bridge_connected"
    BRIDGE_DISCONNECTED = "bridge_disconnected"


@dataclass
class Message:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    source: str = ""
    target: str = ""
    event_type: EventType = EventType.MESSAGE
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    priority: int = 3
    requires_response: bool = False
    parent_id: Optional[str] = None


@dataclass
class Handoff:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    from_agent: str = ""
    to_agent: str = ""
    task: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class EventBus:
    """
    Bus de eventos. Si hay un RealtimeHub global, delega a él.
    Si no, funciona en modo legacy in-process.

    API compatible con la original:
      subscribe(event_type, callback)
      unsubscribe(event_type, callback)
      publish(message)
      enqueue(message)
      process_queue()
      stop()
      get_history(limit)
      get_stats()
    """

    def __init__(self, hub=None):
        self._hub = hub
        self._legacy = _LegacyEventBus() if hub is None else None
        self._history: List[Message] = []
        self._max_history = 1000
        self._hub_sub_ids: List[str] = []

    @property
    def _subscribers(self):
        return self._legacy._subscribers if self._legacy else {}

    @property
    def _message_queue(self):
        return self._legacy._message_queue if self._legacy else type('Q', (), {'qsize': lambda: 0})()

    async def subscribe(self, event_type: str, callback: Callable):
        if self._hub:
            scope = f"event:{event_type}"
            async def wrapper(event):
                msg = Message(
                    source=event.source,
                    target=event.metadata.get("target", "*"),
                    event_type=EventType(event.type) if event.type in [e.value for e in EventType] else EventType.SYSTEM,
                    content=str(event.content) if event.content else "",
                    metadata=event.metadata,
                )
                if asyncio.iscoroutinefunction(callback):
                    await callback(msg)
                else:
                    callback(msg)
            await self._hub.subscribe(scope, wrapper)
            self._hub_sub_ids.append(scope)
        else:
            self._legacy.subscribe(event_type, callback)

    async def unsubscribe(self, event_type: str, callback: Callable):
        if self._hub:
            scope = f"event:{event_type}"
            if scope in self._hub_sub_ids:
                self._hub.unsubscribe(scope)
                self._hub_sub_ids.remove(scope)
        else:
            self._legacy.unsubscribe(event_type, callback)

    async def publish(self, message: Message):
        self._history.append(message)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        # Bridge to the new event_stream (commit 3dc237e) so dashboards
        # subscribing to the canonical bus see legacy producers too. Best-
        # effort: a failure here NEVER blocks the legacy publish path.
        try:
            from src.observability.event_stream import emit as _emit, EventType as _ET
            # Legacy MESSAGE → custom; HANDOFF / TASK_* / LEARNING / etc. don't
            # have a canonical 1:1 in the new enum, so they all surface under
            # CUSTOM with the original type echoed in data.
            _emit(
                _ET.CUSTOM,
                data={
                    "legacy_type": message.event_type.value,
                    "source": message.source,
                    "target": message.target,
                    "content": (message.content or "")[:500],
                    "metadata": message.metadata,
                    "priority": message.priority,
                },
                source="event_bus_legacy",
            )
        except Exception:
            pass

        if self._hub:
            await self._hub.publish(
                event_type=message.event_type.value,
                content=message.content,
                scope=f"event:{message.event_type.value}",
                source=message.source,
                metadata={"target": message.target, **message.metadata},
            )
        else:
            await self._legacy.publish(message)

    async def enqueue(self, message: Message):
        if self._legacy:
            await self._legacy._message_queue.put(message)

    async def process_queue(self):
        if self._legacy:
            await self._legacy.process_queue()

    def stop(self):
        if self._legacy:
            self._legacy.stop()

    def get_history(self, limit: int = 50) -> List[Message]:
        return self._history[-limit:]

    def get_stats(self) -> Dict:
        by_type = defaultdict(int)
        for msg in self._history:
            by_type[msg.event_type.value] += 1
        stats = {
            "total_messages": len(self._history),
            "queue_size": self._message_queue.qsize() if self._legacy else 0,
            "by_type": dict(by_type),
            "mode": "hub" if self._hub else "legacy",
        }
        if self._legacy:
            stats["subscribers"] = {k: len(v) for k, v in self._legacy._subscribers.items()}
        return stats


class _LegacyEventBus:
    """Implementación legacy in-process, usada solo cuando no hay Hub"""

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    def subscribe(self, event_type: str, callback: Callable):
        self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable):
        if callback in self._subscribers[event_type]:
            self._subscribers[event_type].remove(callback)

    async def publish(self, message: Message):
        handlers = self._subscribers.get(message.event_type.value, [])
        handlers.extend(self._subscribers.get("*", []))
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(message)
                else:
                    handler(message)
            except Exception as e:
                logger.error(f"Event handler error: {e}")

    async def process_queue(self):
        self._running = True
        while self._running:
            try:
                message = self._message_queue.get_nowait()
                await self.publish(message)
                self._message_queue.task_done()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.5)
                continue
            except Exception as e:
                logger.error(f"Queue processing error: {e}")

    def stop(self):
        self._running = False


class MessageQueue:
    """Cola de mensajes con prioridades y retry"""

    def __init__(self, max_retries: int = 3):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._max_retries = max_retries
        self._failed: List[Message] = []
        self._processed = 0

    async def put(self, message: Message):
        await self._queue.put((message.priority, message.id, message))

    async def get(self) -> Optional[Message]:
        try:
            _, _, message = self._queue.get_nowait()
            return message
        except asyncio.QueueEmpty:
            return None

    def mark_failed(self, message: Message):
        retries = message.metadata.get("retries", 0)
        if retries < self._max_retries:
            message.metadata["retries"] = retries + 1
            message.priority = max(1, message.priority - 1)
            asyncio.create_task(self._queue.put((message.priority, message.id, message)))
        else:
            self._failed.append(message)
            logger.warning(f"Message failed after {self._max_retries} retries: {message.id}")

    def get_stats(self) -> Dict:
        return {
            "pending": self._queue.qsize(),
            "processed": self._processed,
            "failed": len(self._failed),
        }
