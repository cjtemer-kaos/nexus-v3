"""
Scoped Event Bus - Scoped event system with dedup and handlers.
Absorbed from multica pattern — names cleaned.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Set

logger = logging.getLogger(__name__)


@dataclass
class ScopedEvent:
    event_type: str
    payload: Dict = field(default_factory=dict)
    scope: str = ""
    event_id: str = ""
    timestamp: float = field(default_factory=time.time)
    source: str = ""


class ScopedEventBus:
    """Scoped event bus with dedup and async handlers."""

    def __init__(self, dedup_window: int = 128):
        self._handlers: Dict[str, List[Callable]] = defaultdict(list)
        self._global_handlers: List[Callable] = []
        self._seen_ids: Set[str] = set()
        self._dedup_window = dedup_window
        self._event_history: List[str] = []

    def subscribe(self, event_type: str, handler: Callable):
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: Callable):
        self._global_handlers.append(handler)

    def unsubscribe(self, event_type: str, handler: Callable):
        if handler in self._handlers[event_type]:
            self._handlers[event_type].remove(handler)

    def publish(self, event: ScopedEvent):
        if event.event_id:
            if event.event_id in self._seen_ids:
                return
            self._seen_ids.add(event.event_id)
            self._event_history.append(event.event_id)
            if len(self._event_history) > self._dedup_window:
                old = self._event_history[:self._dedup_window // 2]
                for eid in old:
                    self._seen_ids.discard(eid)
                self._event_history = self._event_history[self._dedup_window // 2:]

        for handler in self._handlers.get(event.event_type, []):
            self._safe_call(handler, event)
        for handler in self._global_handlers:
            self._safe_call(handler, event)

    async def publish_async(self, event: ScopedEvent):
        if event.event_id:
            if event.event_id in self._seen_ids:
                return
            self._seen_ids.add(event.event_id)
            self._event_history.append(event.event_id)
            if len(self._event_history) > self._dedup_window:
                old = self._event_history[:self._dedup_window // 2]
                for eid in old:
                    self._seen_ids.discard(eid)
                self._event_history = self._event_history[self._dedup_window // 2:]

        for handler in self._handlers.get(event.event_type, []):
            await self._safe_call_async(handler, event)
        for handler in self._global_handlers:
            await self._safe_call_async(handler, event)

    def _safe_call(self, handler: Callable, event: ScopedEvent):
        try:
            handler(event)
        except Exception as e:
            logger.error(f"Event handler error ({event.event_type}): {e}")

    async def _safe_call_async(self, handler: Callable, event: ScopedEvent):
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(event)
            else:
                handler(event)
        except Exception as e:
            logger.error(f"Event handler error ({event.event_type}): {e}")

    def clear(self):
        self._handlers.clear()
        self._global_handlers.clear()
        self._seen_ids.clear()
        self._event_history.clear()

    @property
    def stats(self) -> Dict:
        return {
            "event_types": len(self._handlers),
            "total_handlers": sum(len(h) for h in self._handlers.values()) + len(self._global_handlers),
            "seen_events": len(self._seen_ids),
        }
