# src/core/dream_consolidation.py
"""
Dream Consolidation — Batch episodic->semantic promotion.

Runs during idle time or on schedule:
1. Select: episodic memories with high utility + high access count
2. Extract: entities, tags, patterns from selected memories
3. Promote: move to semantic tier with enriched metadata
4. Snapshot: JSON backup of memory state (git-versionable)
5. Prune: optionally remove promoted episodics to save space

Inspired by how human brains consolidate short-term memories
during sleep into long-term storage.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class DreamConfig:
    min_importance: float = 0.5
    min_access_count: int = 5
    max_age_hours: float = 168.0  # 7 days — only consolidate after settling
    prune_after_consolidation: bool = False
    snapshot_dir: str | None = None  # if set, create JSON snapshots


@dataclass
class DreamResult:
    selected: int = 0
    promoted: int = 0
    pruned: int = 0
    snapshot_path: str | None = None
    duration_s: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def summary(self) -> str:
        parts = [
            f"{self.selected} selected",
            f"{self.promoted} promoted",
            f"{self.pruned} pruned",
        ]
        if self.snapshot_path:
            parts.append(f"snapshot: {self.snapshot_path}")
        return "Dream consolidation: " + ", ".join(parts)


class DreamConsolidator:
    """Batch process for episodic->semantic memory promotion."""

    def __init__(self, config: DreamConfig | None = None):
        self.config = config or DreamConfig()

    def select(self, memory) -> list:
        """Select episodic memories worth promoting to semantic."""
        candidates = []
        for item in memory._items:
            if item.tier != "episodic":
                continue
            if item.importance < self.config.min_importance:
                continue
            if item.access_count < self.config.min_access_count:
                continue
            candidates.append(item)

        # Sort by combined score: importance * access_count
        candidates.sort(key=lambda x: x.importance * x.access_count, reverse=True)
        return candidates

    def consolidate(self, memory) -> DreamResult:
        """Run full dream consolidation cycle."""
        t0 = time.time()
        result = DreamResult()

        # 1. Select candidates
        candidates = self.select(memory)
        result.selected = len(candidates)

        if not candidates:
            result.duration_s = time.time() - t0
            return result

        # 2. Promote to semantic
        for item in candidates:
            item.tier = "semantic"
            # Boost importance for promoted items
            item.importance = min(1.0, item.importance + 0.1)
            result.promoted += 1

        # 3. Snapshot before pruning
        if self.config.snapshot_dir:
            result.snapshot_path = self._create_snapshot(memory)

        # 4. Prune (optional) — remove low-value episodics that weren't selected
        if self.config.prune_after_consolidation:
            before = len(memory._items)
            memory._items = [
                i for i in memory._items
                if not (i.tier == "episodic" and i.importance < 0.2 and i.access_count < 2)
            ]
            result.pruned = before - len(memory._items)

        # 5. Save
        memory._save()
        result.duration_s = time.time() - t0
        logger.info(result.summary())
        return result

    def _create_snapshot(self, memory) -> str:
        """Create JSON snapshot of current memory state."""
        snap_dir = Path(self.config.snapshot_dir)
        snap_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_path = snap_dir / f"memory_snapshot_{timestamp}.json"

        data = {
            "timestamp": datetime.now().isoformat(),
            "total_items": len(memory._items),
            "tiers": {},
            "items": [item.to_dict() for item in memory._items],
        }
        # Count by tier
        for item in memory._items:
            data["tiers"][item.tier] = data["tiers"].get(item.tier, 0) + 1

        atomic_write_json(snap_path, data)

        return str(snap_path)

    def status(self) -> dict:
        return {
            "config": {
                "min_importance": self.config.min_importance,
                "min_access_count": self.config.min_access_count,
                "prune": self.config.prune_after_consolidation,
                "snapshot_dir": self.config.snapshot_dir,
            },
        }
