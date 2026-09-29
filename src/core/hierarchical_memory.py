from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from src.core.memory_triage import MemoryTriage
from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_MEMORY_STORAGE = Path.home() / ".nexus" / "hierarchical_memory.json"


class MemoryTier(Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


@dataclass
class MemoryItem:
    id: str = ""
    content: str = ""
    tier: str = "working"
    tags: list[str] = field(default_factory=list)
    source: str = ""
    importance: float = 0.5
    access_count: int = 0
    created_at: float = 0.0
    last_access: float = 0.0

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    @property
    def decay_score(self) -> float:
        hours = self.age_s / 3600.0
        recency = math.exp(-hours / self._tier_halflife_hours())
        frequency = 1.0 - math.exp(-self.access_count / 10.0)
        return recency * 0.7 + frequency * 0.3

    def _tier_halflife_hours(self) -> float:
        return {"working": 1.0, "episodic": 72.0, "semantic": 8760.0}.get(self.tier, 1.0)

    def promote(self) -> str:
        return {"working": "episodic", "episodic": "semantic", "semantic": "semantic"}.get(self.tier, self.tier)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "content": self.content[:500], "tier": self.tier,
            "tags": self.tags, "source": self.source, "importance": self.importance,
            "access_count": self.access_count, "created_at": self.created_at,
            "last_access": self.last_access,
        }

    @classmethod
    def from_dict(cls, d: dict) -> MemoryItem:
        return cls(**{
            k: d.get(k, v.default if hasattr(v, 'default') else None)
            for k, v in cls.__dataclass_fields__.items()
        })


_singleton_instance: HierarchicalMemory | None = None
_singleton_lock = __import__('threading').Lock()


def get_memory() -> HierarchicalMemory:
    """Get or create the singleton HierarchicalMemory instance (thread-safe)."""
    global _singleton_instance
    if _singleton_instance is not None:
        return _singleton_instance
    with _singleton_lock:
        if _singleton_instance is None:
            _singleton_instance = HierarchicalMemory()
    return _singleton_instance


class HierarchicalMemory:
    def __init__(self, working_capacity: int = 100, episodic_capacity: int = 500,
                 triage: MemoryTriage | None = None):
        self._items: list[MemoryItem] = []
        self._working_capacity = working_capacity
        self._episodic_capacity = episodic_capacity
        self._triage = triage or MemoryTriage()
        self._triage_rejections = 0
        self._load()

    def store(self, content: str, tags: list[str] | None = None,
              source: str = "", importance: float = 0.5,
              tier: str = "working", bypass_triage: bool = False) -> MemoryItem | None:
        if not bypass_triage:
            triage_result = self._triage.evaluate(content)
            if not triage_result.passed:
                self._triage_rejections += 1
                logger.debug(f"Memory triage rejected: {triage_result.reason}")
                return None

        item = MemoryItem(
            id=f"mem_{int(time.time() * 1000)}_{len(self._items)}",
            content=content, tier=tier, tags=tags or [],
            source=source, importance=importance,
            created_at=time.time(), last_access=time.time(),
        )
        self._items.append(item)
        self._enforce_capacity()
        self._save()
        return item

    def search(self, query: str, tier: str | None = None,
               min_importance: float = 0.0, top_k: int = 10) -> list[MemoryItem]:
        query_lower = query.lower()
        query_words = set(query_lower.split())

        scored = []
        for item in self._items:
            if tier and item.tier != tier:
                continue
            if item.importance < min_importance:
                continue

            score = 0.0
            content_lower = item.content.lower()

            for word in query_words:
                if len(word) < 3:
                    continue
                if word in content_lower:
                    score += 1.0
                elif any(w.startswith(word[:4]) for w in content_lower.split()):
                    score += 0.5

            for tag in item.tags:
                if tag.lower() in query_lower:
                    score += 2.0

            if score > 0:
                score *= (1.0 + item.importance * 0.5)
                score *= item.decay_score
                scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)

        for _, item in scored[:top_k]:
            item.access_count += 1
            item.last_access = time.time()
        self._save()

        return [item for _, item in scored[:top_k]]

    def consolidate(self, summarizer: Callable | None = None):
        now = time.time()
        moved = 0

        for item in list(self._items):
            if item.tier == "working":
                hours = (now - item.created_at) / 3600.0
                if hours > 2.0 and item.access_count < 3:
                    new_tier = item.promote()
                    if summarizer:
                        try:
                            summary = summarizer(item.content)
                            item.content = summary[:500]
                        except Exception:
                            pass
                    item.tier = new_tier
                    moved += 1

        # Prune low-importance items from working memory
        working = [i for i in self._items if i.tier == "working"]
        if len(working) > self._working_capacity:
            working.sort(key=lambda i: i.decay_score)
            for item in working[:len(working) - self._working_capacity]:
                if item.access_count == 0:
                    self._items.remove(item)

        episodic = [i for i in self._items if i.tier == "episodic"]
        if len(episodic) > self._episodic_capacity:
            episodic.sort(key=lambda i: i.decay_score)
            for item in episodic[:len(episodic) - self._episodic_capacity]:
                if item.access_count == 0:
                    self._items.remove(item)

        if moved:
            logger.info("HierarchicalMemory: consolidated %d items", moved)
            self._save()

    def get_stats(self) -> dict:
        tiers = {}
        for item in self._items:
            tiers.setdefault(item.tier, {"count": 0, "avg_importance": 0.0})
            tiers[item.tier]["count"] += 1
            tiers[item.tier]["avg_importance"] += item.importance
        for t in tiers.values():
            t["avg_importance"] = round(t["avg_importance"] / max(t["count"], 1), 2)
        return {
            "total": len(self._items),
            "tiers": tiers,
            "working_capacity": self._working_capacity,
            "episodic_capacity": self._episodic_capacity,
        }

    def _enforce_capacity(self):
        working = [i for i in self._items if i.tier == "working"]
        if len(working) > self._working_capacity:
            working.sort(key=lambda i: (i.importance, i.decay_score))
            for item in working[:len(working) - self._working_capacity]:
                item.tier = "episodic"
                item.content = item.content[:300]

    def _save(self):
        try:
            data = {
                "items": [i.to_dict() for i in self._items],
                "updated_at": time.time(),
            }
            _MEMORY_STORAGE.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_MEMORY_STORAGE, data)
        except Exception as e:
            logger.debug("HierarchicalMemory save failed: %s", e)

    def _load(self):
        try:
            if _MEMORY_STORAGE.exists():
                with open(_MEMORY_STORAGE) as f:
                    data = json.load(f)
                self._items = [MemoryItem.from_dict(d) for d in data.get("items", [])]
                logger.info("Loaded hierarchical memory: %d items", len(self._items))
        except Exception as e:
            logger.debug("HierarchicalMemory load failed: %s", e)
