"""
MessageBus - Canal de comunicacion async inter-gemas para SuperNEXUS v2

Permite que las gemas se comuniquen entre si de forma estructurada:
- publish/subscribe por topicos
- Mensajes tipo request/response
- Broadcast a todas las gemas
- Cola de mensajes persistente por gema

Patrones:
- Pub/Sub con filtros por topic y gema
- Request/Response con correlation ID
- Message routing con prioridad
- Dead letter queue para mensajes fallidos
"""

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("nexus-messagebus")


class MessageType(Enum):
    PUBLISH = "publish"
    REQUEST = "request"
    RESPONSE = "response"
    BROADCAST = "broadcast"
    EVENT = "event"
    ERROR = "error"


class MessagePriority(Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class BusMessage:
    """Mensaje en el bus"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: MessageType = MessageType.PUBLISH
    source: str = ""
    target: str = "*"  # "*" = broadcast, o nombre de gema especifica
    topic: str = ""
    content: Any = None
    priority: MessagePriority = MessagePriority.NORMAL
    correlation_id: str = ""  # Para request/response
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    ttl_seconds: float = 60  # Time to live
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        created = datetime.fromisoformat(self.timestamp)
        return (datetime.now() - created).total_seconds() > self.ttl_seconds

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "source": self.source,
            "target": self.target,
            "topic": self.topic,
            "content": self.content,
            "priority": self.priority.value,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "BusMessage":
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            type=MessageType(data.get("type", "publish")),
            source=data.get("source", ""),
            target=data.get("target", "*"),
            topic=data.get("topic", ""),
            content=data.get("content"),
            priority=MessagePriority(data.get("priority", 1)),
            correlation_id=data.get("correlation_id", ""),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
            ttl_seconds=data.get("ttl_seconds", 60),
            metadata=data.get("metadata", {}),
        )


@dataclass
class Subscription:
    """Suscripcion a un topic"""
    subscriber_id: str
    topic: str
    callback: Callable
    filter_fn: Callable = None  # Filtro opcional
    created_at: float = field(default_factory=time.time)


class MessageBus:
    """
    Bus de mensajes async para comunicacion inter-gemas.

    Uso:
        bus = MessageBus()
        await bus.start()

        # Suscribirse a un topic
        await bus.subscribe("code_gema", "code.*", handler)

        # Publicar mensaje
        await bus.publish("code_gema", "code.refactor", {"file": "main.py"})

        # Request/Response
        response = await bus.request("code_gema", "debugger", "debug.analyze", {"error": "..."})

        # Broadcast
        await bus.broadcast("director", "system.shutdown", {"reason": "maintenance"})
    """

    def __init__(self, max_queue_size: int = 1000, default_ttl: float = 60):
        self._subscriptions: Dict[str, List[Subscription]] = defaultdict(list)  # topic -> [subscriptions]
        self._queues: Dict[str, asyncio.Queue] = {}  # subscriber_id -> queue
        self._pending_requests: Dict[str, asyncio.Future] = {}  # correlation_id -> future
        self._running = False
        self._max_queue_size = max_queue_size
        self._default_ttl = default_ttl
        self._stats = {
            "messages_published": 0,
            "messages_delivered": 0,
            "messages_expired": 0,
            "messages_dropped": 0,
            "requests_pending": 0,
        }
        self._dead_letter_queue: deque = deque(maxlen=100)
        self._dispatch_task: Optional[asyncio.Task] = None

    async def start(self):
        """Inicia el bus de mensajes"""
        self._running = True
        logger.info("MessageBus started")

    async def stop(self):
        """Detiene el bus de mensajes"""
        self._running = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
        # Resolver pending requests como cancelados
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(asyncio.CancelledError("MessageBus stopped"))
        self._pending_requests.clear()
        logger.info("MessageBus stopped")

    async def subscribe(self, subscriber_id: str, topic_pattern: str, callback: Callable, filter_fn: Callable = None):
        """
        Suscribe una gema a un topic.

        topic_pattern soporta wildcards:
        - "code.*" → todos los subtopicos de code
        - "code.refactor" → solo refactor
        - "*" → todos los mensajes
        """
        subscription = Subscription(
            subscriber_id=subscriber_id,
            topic=topic_pattern,
            callback=callback,
            filter_fn=filter_fn,
        )
        self._subscriptions[topic_pattern].append(subscription)

        # Crear cola si no existe
        if subscriber_id not in self._queues:
            self._queues[subscriber_id] = asyncio.Queue(maxsize=self._max_queue_size)

        logger.info(f"Subscribed: {subscriber_id} -> {topic_pattern}")

    async def unsubscribe(self, subscriber_id: str, topic_pattern: str = None):
        """Elimina suscripcion"""
        if topic_pattern:
            self._subscriptions[topic_pattern] = [
                s for s in self._subscriptions[topic_pattern]
                if s.subscriber_id != subscriber_id
            ]
        else:
            for topic in list(self._subscriptions.keys()):
                self._subscriptions[topic] = [
                    s for s in self._subscriptions[topic]
                    if s.subscriber_id != subscriber_id
                ]

    async def publish(self, source: str, topic: str, content: Any, priority: MessagePriority = MessagePriority.NORMAL, ttl: float = None):
        """Publica un mensaje en el bus"""
        msg = BusMessage(
            type=MessageType.PUBLISH,
            source=source,
            target="*",
            topic=topic,
            content=content,
            priority=priority,
            ttl_seconds=ttl or self._default_ttl,
        )
        await self._dispatch(msg)

    async def request(self, source: str, target: str, topic: str, content: Any, timeout: float = 30) -> Any:
        """
        Envio request/response.

        Espera respuesta del target con timeout.
        """
        correlation_id = str(uuid.uuid4())[:12]
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[correlation_id] = future
        self._stats["requests_pending"] += 1

        msg = BusMessage(
            type=MessageType.REQUEST,
            source=source,
            target=target,
            topic=topic,
            content=content,
            correlation_id=correlation_id,
            priority=MessagePriority.HIGH,
            ttl_seconds=timeout,
        )

        await self._dispatch(msg)

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            self._pending_requests.pop(correlation_id, None)
            self._stats["requests_pending"] -= 1
            raise TimeoutError(f"Request timeout: {topic} -> {target}")

    async def respond(self, correlation_id: str, content: Any):
        """Envia respuesta a un request pendiente"""
        if correlation_id in self._pending_requests:
            future = self._pending_requests.pop(correlation_id)
            self._stats["requests_pending"] -= 1
            if not future.done():
                future.set_result(content)

    async def broadcast(self, source: str, topic: str, content: Any):
        """Envia mensaje a todas las gemas"""
        msg = BusMessage(
            type=MessageType.BROADCAST,
            source=source,
            target="*",
            topic=topic,
            content=content,
            priority=MessagePriority.HIGH,
        )
        await self._dispatch(msg)

    async def send_event(self, source: str, topic: str, content: Any):
        """Envio de evento (no requiere respuesta)"""
        msg = BusMessage(
            type=MessageType.EVENT,
            source=source,
            target="*",
            topic=topic,
            content=content,
        )
        await self._dispatch(msg)

    async def _dispatch(self, msg: BusMessage):
        """Despacha mensaje a los suscriptores correspondientes"""
        if msg.is_expired:
            self._stats["messages_expired"] += 1
            return

        self._stats["messages_published"] += 1
        delivered = 0

        # Encontrar suscriptores matching
        matching_subs = self._find_subscribers(msg)

        for sub in matching_subs:
            # Aplicar filtro si existe
            if sub.filter_fn and not sub.filter_fn(msg):
                continue

            # Entregar a la cola del suscriptor
            queue = self._queues.get(sub.subscriber_id)
            if queue:
                try:
                    queue.put_nowait(msg)
                    delivered += 1
                    self._stats["messages_delivered"] += 1
                except asyncio.QueueFull:
                    self._stats["messages_dropped"] += 1
                    self._dead_letter_queue.append(msg.to_dict())
                    logger.warning(f"Queue full for {sub.subscriber_id}, message dropped")

        # Si es request y no hay suscriptores, error
        if msg.type == MessageType.REQUEST and delivered == 0:
            error_msg = f"No subscriber for request: {msg.topic} -> {msg.target}"
            logger.warning(error_msg)
            if msg.correlation_id in self._pending_requests:
                future = self._pending_requests.pop(msg.correlation_id)
                if not future.done():
                    future.set_exception(RuntimeError(error_msg))

    def _find_subscribers(self, msg: BusMessage) -> List[Subscription]:
        """Encuentra suscriptores matching para un mensaje"""
        matching = []

        for pattern, subs in self._subscriptions.items():
            if self._matches_topic(msg.topic, pattern):
                for sub in subs:
                    # Si el mensaje es dirigido a una gema especifica, filtrar
                    if msg.target != "*" and msg.target != sub.subscriber_id:
                        continue
                    matching.append(sub)

        return matching

    @staticmethod
    def _matches_topic(topic: str, pattern: str) -> bool:
        """Verifica si un topic matchea un patron con wildcards"""
        if pattern == "*":
            return True

        pattern_parts = pattern.split(".")
        topic_parts = topic.split(".")

        if len(pattern_parts) != len(topic_parts):
            return False

        for p, t in zip(pattern_parts, topic_parts):
            if p != "*" and p != t:
                return False

        return True

    async def get_next_message(self, subscriber_id: str, timeout: float = None) -> Optional[BusMessage]:
        """Obtiene el siguiente mensaje de la cola de un suscriptor"""
        queue = self._queues.get(subscriber_id)
        if not queue:
            return None

        try:
            if timeout:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        return queue.get_nowait()
                    except asyncio.QueueEmpty:
                        if time.monotonic() >= deadline:
                            return None
                        await asyncio.sleep(0.2)
            else:
                return queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def get_stats(self) -> Dict:
        return {
            **self._stats,
            "subscribers": len(self._queues),
            "topics": len(self._subscriptions),
            "dead_letter_queue_size": len(self._dead_letter_queue),
        }

    def get_dead_letters(self, limit: int = 10) -> List[Dict]:
        """Obtiene mensajes de la dead letter queue"""
        return list(self._dead_letter_queue)[-limit:]
