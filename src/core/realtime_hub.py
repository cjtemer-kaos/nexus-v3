"""
RealtimeHub — 3-capas: Hub → Broadcaster → Redis Streams con scopes + dedup

Arquitectura:
  Hub (RealtimeHub)
   ├── Broadcaster (scope-based pub/sub)
   │    ├── scope "workspace:{id}" → eventos de workspace
   │    ├── scope "task:{id}"      → eventos de task
   │    ├── scope "agent:{name}"   → eventos de agente
   │    └── scope "system"         → eventos de sistema
   ├── DedupEngine (sliding window, last 1000 IDs)
   └── Transports
        ├── Local (asyncio.Queue — siempre activo)
        └── Redis Streams (async, solo si redis está disponible)

Uso:
    hub = RealtimeHub(message_bus, nexus_hive)
    await hub.start()

    # Scope subscribe
    await hub.subscribe("workspace:default", on_workspace_event)
    await hub.subscribe("task:*", on_task_event)  # wildcard

    # Publish with scope + dedup
    await hub.publish("task.complete", {"id": "abc"}, scope="task:abc123")

    # Dual-write: local inmediato + async a Redis
    # Si Redis está caído, solo local (sin bloqueo)
"""

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("nexus-realtime-hub")


def _ulid() -> str:
    """Universally Unique Lexicographically Sortable Identifier"""
    timestamp = int(time.time() * 1000)
    random_part = uuid.uuid4().hex[:16]
    return f"{timestamp:012x}{random_part}"


class ScopePattern:
    """
    Hierarchical scope matching with wildcard support.
    "task:*" matches "task:abc123"
    "*" matches everything
    "agent:scholar" only matches exact "agent:scholar"
    """

    def __init__(self):
        self._cache: Dict[str, List[str]] = {}

    def matches(self, pattern: str, scope: str) -> bool:
        if pattern == "*":
            return True
        if pattern == scope:
            return True
        if pattern.endswith(":*"):
            prefix = pattern[:-2]
            return scope.startswith(prefix + ":")
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            return scope.startswith(prefix + ".")
        return False

    def normalize(self, scope: str) -> str:
        return scope.strip().lower()


@dataclass
class HubEvent:
    """Evento unificado del sistema"""
    id: str = field(default_factory=_ulid)
    type: str = ""                      # task.complete, agent.status, system.shutdown
    scope: str = "system"              # workspace:x, task:y, agent:z, system
    source: str = ""                    # "director", "scholar_gem", "nexus_hive"
    content: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    ttl_seconds: float = 300

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.timestamp) > self.ttl_seconds

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "type": self.type,
            "scope": self.scope,
            "source": self.source,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
            "ttl": self.ttl_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "HubEvent":
        return cls(
            id=data.get("id", _ulid()),
            type=data.get("type", ""),
            scope=data.get("scope", "system"),
            source=data.get("source", ""),
            content=data.get("content"),
            metadata=data.get("metadata", {}),
            timestamp=data.get("timestamp", time.time()),
            ttl_seconds=data.get("ttl", 300),
        )


class DedupEngine:
    """
    Sliding window dedup. Mantiene los últimos N event_ids vistos.
    Window: 1000 entries (aprox 16KB para 1000 ULIDs)
    """

    def __init__(self, window_size: int = 1000):
        self._window: deque = deque(maxlen=window_size)
        self._seen: Set[str] = set()

    def is_duplicate(self, event_id: str) -> bool:
        return event_id in self._seen

    def mark_seen(self, event_id: str):
        if event_id in self._seen:
            return
        if len(self._window) == self._window.maxlen:
            oldest = self._window.popleft()
            self._seen.discard(oldest)
        self._window.append(event_id)
        self._seen.add(event_id)

    def clear(self):
        self._window.clear()
        self._seen.clear()

    @property
    def size(self) -> int:
        return len(self._window)


@dataclass
class ScopeSubscriber:
    """Suscripcion con scope"""
    id: str
    scope_pattern: str
    callback: Callable
    filter_fn: Optional[Callable] = None
    created_at: float = field(default_factory=time.time)


class Broadcaster:
    """
    Scope-based pub/sub con wildcards.
    """

    def __init__(self):
        self._subscribers: Dict[str, List[ScopeSubscriber]] = defaultdict(list)
        self._scope_matcher = ScopePattern()
        self._stats = {
            "subscribers_total": 0,
            "events_published": 0,
            "events_delivered": 0,
        }

    def subscribe(self, scope_pattern: str, callback: Callable, filter_fn: Optional[Callable] = None) -> str:
        sub_id = _ulid()[:12]
        sub = ScopeSubscriber(
            id=sub_id,
            scope_pattern=self._scope_matcher.normalize(scope_pattern),
            callback=callback,
            filter_fn=filter_fn,
        )
        self._subscribers[sub.scope_pattern].append(sub)
        self._stats["subscribers_total"] += 1
        return sub_id

    def unsubscribe(self, sub_id: str):
        for pattern, subs in list(self._subscribers.items()):
            self._subscribers[pattern] = [s for s in subs if s.id != sub_id]
            if not self._subscribers[pattern]:
                del self._subscribers[pattern]
        self._stats["subscribers_total"] = sum(len(v) for v in self._subscribers.values())

    async def deliver(self, event: HubEvent) -> int:
        """Entrega un evento a todos los suscriptores cuyo scope matchee"""
        self._stats["events_published"] += 1
        delivered = 0

        for pattern, subs in list(self._subscribers.items()):
            if not self._scope_matcher.matches(pattern, event.scope):
                continue
            for sub in subs:
                if sub.filter_fn and not sub.filter_fn(event):
                    continue
                try:
                    if asyncio.iscoroutinefunction(sub.callback):
                        await sub.callback(event)
                    else:
                        sub.callback(event)
                    delivered += 1
                    self._stats["events_delivered"] += 1
                except Exception as e:
                    logger.error(f"Subscriber {sub.id} error: {e}")

        return delivered

    def get_stats(self) -> Dict:
        return {
            **self._stats,
            "patterns": list(self._subscribers.keys()),
        }


class RealtimeHub:
    """
    Hub de eventos en tiempo real. 3 capas:

    1. DedupEngine — sliding window previene eventos duplicados
    2. Broadcaster — scope-based pub/sub con wildcards
    3. Transportes — local (in-process) + Redis Streams (multi-node)

    Dual-write: siempre escribe local primero, luego async a Redis.
    Si Redis está caído, no bloquea — solo entrega local.
    """

    REDIS_STREAM_KEY = "nexus:events"
    REDIS_CONSUMER_GROUP = "nexus:hub"

    def __init__(
        self,
        message_bus=None,
        nexus_hive=None,
        event_store=None,
        dedup_window: int = 1000,
    ):
        self.message_bus = message_bus
        self.nexus_hive = nexus_hive
        self.event_store = event_store
        self.broadcaster = Broadcaster()
        self.dedup = DedupEngine(window_size=dedup_window)
        self._running = False
        self._redis_consumer_task: Optional[asyncio.Task] = None
        self._bridge_task: Optional[asyncio.Task] = None
        self._stats = {
            "events_published": 0,
            "events_duplicates_skipped": 0,
            "remote_broadcast": 0,
            "remote_received": 0,
            "local_delivered": 0,
        }

    async def start(self):
        """Inicia el Hub (idempotente)"""
        if self._running:
            return
        self._running = True

        # Bridge: escuchar mensajes de MessageBus y re-publicarlos como HubEvents
        if self.message_bus:
            self._bridge_task = asyncio.create_task(self._bridge_from_messagebus())

        # Redis consumer: escuchar eventos de otros nodos
        redis_available = self.nexus_hive and self.nexus_hive.redis
        if redis_available:
            self._redis_consumer_task = asyncio.create_task(self._consume_redis_streams())
            logger.info("RealtimeHub: Redis consumer activo")
        else:
            logger.info("RealtimeHub: solo modo local (sin Redis)")

        logger.info("RealtimeHub started")

    async def stop(self):
        """Detiene el Hub"""
        self._running = False
        if self._redis_consumer_task:
            self._redis_consumer_task.cancel()
        if self._bridge_task:
            self._bridge_task.cancel()
        logger.info("RealtimeHub stopped")

    async def publish(
        self,
        event_type: str,
        content: Any,
        scope: str = "system",
        source: str = "",
        metadata: Optional[Dict] = None,
    ) -> HubEvent:
        """
        Publica un evento con dedup + dual-write.

        1. Crea HubEvent con ULID único
        2. Dedup check (si ya se vio este event_id, skip)
        3. Entrega local via Broadcaster (scope-based)
        4. Persiste en EventStore si disponible
        5. Broadcast async a Redis Streams si disponible
        """
        event = HubEvent(
            type=event_type,
            scope=scope,
            source=source or "realtime_hub",
            content=content,
            metadata=metadata or {},
        )

        if self.dedup.is_duplicate(event.id):
            self._stats["events_duplicates_skipped"] += 1
            return event

        self.dedup.mark_seen(event.id)
        self._stats["events_published"] += 1

        # 1. Local delivery via Broadcaster
        delivered = await self.broadcaster.deliver(event)
        self._stats["local_delivered"] += delivered

        # 2. MessageBus bridge (reenviar como BusMessage si hay bus)
        if self.message_bus:
            try:
                await self.message_bus.publish(
                    source=event.source,
                    topic=event.type,
                    content=event.to_dict(),
                )
            except Exception as e:
                logger.warning(f"MessageBus publish error: {e}")

        # 3. EventStore persistence (async, no bloquea)
        if self.event_store:
            asyncio.create_task(self._persist_event(event))

        # 4. Redis broadcast (async, no bloquea)
        if self.nexus_hive and self.nexus_hive.redis:
            asyncio.create_task(self._broadcast_to_redis(event))

        return event

    async def subscribe(
        self,
        scope_pattern: str,
        callback: Callable,
        filter_fn: Optional[Callable] = None,
    ) -> str:
        """Suscribe un callback a un scope pattern"""
        return self.broadcaster.subscribe(scope_pattern, callback, filter_fn)

    def unsubscribe(self, sub_id: str):
        """Desuscribe por ID"""
        self.broadcaster.unsubscribe(sub_id)

    async def _broadcast_to_redis(self, event: HubEvent):
        """Broadcast async a Redis Stream"""
        try:
            redis = self.nexus_hive.redis
            payload = json.dumps(event.to_dict())
            await redis.xadd(self.REDIS_STREAM_KEY, {"data": payload}, maxlen=1000)
            self._stats["remote_broadcast"] += 1
        except Exception as e:
            logger.debug(f"Redis broadcast error (non-fatal): {e}")

    async def _consume_redis_streams(self):
        """Consume eventos de Redis Stream de otros nodos"""
        redis = self.nexus_hive.redis
        consumer_id = f"hub:{uuid.uuid4().hex[:8]}"

        try:
            await redis.xgroup_create(
                self.REDIS_STREAM_KEY,
                self.REDIS_CONSUMER_GROUP,
                id="0",
                mkstream=True,
            )
        except Exception:
            pass

        while self._running:
            try:
                results = await redis.xreadgroup(
                    self.REDIS_CONSUMER_GROUP,
                    consumer_id,
                    {self.REDIS_STREAM_KEY: ">"},
                    count=10,
                    block=2000,
                )
                if not results:
                    continue

                for stream, messages in results:
                    for msg_id, msg_data in messages:
                        await self._process_remote_event(msg_data.get("data", "{}"))
                        await redis.xack(self.REDIS_STREAM_KEY, self.REDIS_CONSUMER_GROUP, msg_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Redis consume error (non-fatal): {e}")
                await asyncio.sleep(1)

    async def _process_remote_event(self, raw_data: str):
        """Procesa evento recibido de Redis (de otro nodo)"""
        try:
            data = json.loads(raw_data)
            event = HubEvent.from_dict(data)

            if self.dedup.is_duplicate(event.id):
                self._stats["events_duplicates_skipped"] += 1
                return

            self.dedup.mark_seen(event.id)
            self._stats["remote_received"] += 1

            delivered = await self.broadcaster.deliver(event)
            self._stats["local_delivered"] += delivered

        except Exception as e:
            logger.warning(f"Error processing remote event: {e}")

    async def _persist_event(self, event: HubEvent):
        """Persiste evento en EventStore"""
        try:
            from .event_store import Event, EventKind
            store_event = Event(
                kind=EventKind.CUSTOM,
                data=event.to_dict(),
                conversation_id=event.scope,
            )
            self.event_store.append(store_event)
        except Exception as e:
            logger.debug(f"EventStore persist error: {e}")

    async def _bridge_from_messagebus(self):
        """
        Bridge: escucha MessageBus y re-publica eventos como HubEvents.
        Conecta el sistema legacy de gemas con el nuevo sistema de scopes.
        """
        if not self.message_bus:
            return

        while self._running:
            try:
                msg = await self.message_bus.get_next_message("realtime_hub", timeout=1.0)
                if msg is None:
                    continue

                event = HubEvent(
                    type=msg.topic,
                    scope=f"agent:{msg.source}",
                    source=msg.source,
                    content=msg.content,
                    metadata={"correlation_id": msg.correlation_id, "target": msg.target},
                )

                if not self.dedup.is_duplicate(event.id):
                    self.dedup.mark_seen(event.id)
                    await self.broadcaster.deliver(event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"MessageBus bridge error: {e}")
                await asyncio.sleep(0.1)

    def status(self) -> Dict:
        """Estado completo del Hub"""
        redis_ok = self.nexus_hive and self.nexus_hive.redis is not None
        return {
            "running": self._running,
            "redis_connected": redis_ok,
            "dedup_window": self.dedup.size,
            "stats": {**self._stats},
            "broadcaster": self.broadcaster.get_stats(),
            "transport": "redis+local" if redis_ok else "local-only",
        }
