"""
Snapshot Store — Point-in-time state persistence for the entire runtime.

Pattern extracted from openclaw snapshot-store.ts.
Captures full system state (rooms, tasks, budgets, memory stats) for
rollback, inspection, and recovery purposes.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger("nexus-snapshot")


@dataclass
class Snapshot:
    id: str = ""
    label: str = ""
    created_at: str = ""
    data: Dict = field(default_factory=dict)
    size_bytes: int = 0

    def __post_init__(self):
        import uuid
        if not self.id:
            self.id = str(uuid.uuid4())[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


class SnapshotStore:
    def __init__(self, persist_dir: str = "", max_snapshots: int = 20):
        self._snapshots: Dict[str, Snapshot] = {}
        self._max_snapshots = max_snapshots
        self._persist_dir = persist_dir
        if persist_dir:
            Path(persist_dir).mkdir(parents=True, exist_ok=True)
            self._load_index()

    def _index_path(self) -> str:
        return os.path.join(self._persist_dir, "snapshot_index.json") if self._persist_dir else ""

    def _load_index(self):
        idx_path = self._index_path()
        if not idx_path or not os.path.exists(idx_path):
            return
        try:
            with open(idx_path, "r") as f:
                index = json.load(f)
            for sid in index.get("snapshots", []):
                snap_path = os.path.join(self._persist_dir, f"{sid}.json")
                if os.path.exists(snap_path):
                    with open(snap_path, "r") as f:
                        data = json.load(f)
                    self._snapshots[sid] = Snapshot(
                        id=data["id"],
                        label=data.get("label", ""),
                        created_at=data.get("created_at", ""),
                        data=data.get("data", {}),
                        size_bytes=data.get("size_bytes", 0),
                    )
            logger.info(f"Loaded {len(self._snapshots)} snapshots")
        except Exception as e:
            logger.warning(f"Could not load snapshot index: {e}")

    def _save_snapshot(self, snapshot: Snapshot):
        if not self._persist_dir:
            return
        try:
            snap_path = os.path.join(self._persist_dir, f"{snapshot.id}.json")
            atomic_write_json(snap_path, {
                "id": snapshot.id,
                "label": snapshot.label,
                "created_at": snapshot.created_at,
                "data": snapshot.data,
                "size_bytes": snapshot.size_bytes,
            })
        except Exception as e:
            logger.warning(f"Could not persist snapshot {snapshot.id}: {e}")

    def _save_index(self):
        idx_path = self._index_path()
        if not idx_path:
            return
        try:
            atomic_write_json(idx_path, {
                "updated_at": datetime.now().isoformat(),
                "snapshots": list(self._snapshots.keys()),
            })
        except Exception as e:
            logger.warning(f"Could not save snapshot index: {e}")

    def take(self, label: str, data: Dict) -> Snapshot:
        serialized = json.dumps(data, default=str)
        snapshot = Snapshot(
            label=label,
            data=data,
            size_bytes=len(serialized),
        )
        self._snapshots[snapshot.id] = snapshot
        if len(self._snapshots) > self._max_snapshots:
            oldest = min(self._snapshots.values(), key=lambda s: s.created_at)
            del self._snapshots[oldest.id]
            if self._persist_dir:
                old_path = os.path.join(self._persist_dir, f"{oldest.id}.json")
                if os.path.exists(old_path):
                    os.remove(old_path)
        self._save_snapshot(snapshot)
        self._save_index()
        logger.info(f"Snapshot taken: {snapshot.id} - {label} ({snapshot.size_bytes} bytes)")
        return snapshot

    def get(self, snapshot_id: str) -> Optional[Snapshot]:
        return self._snapshots.get(snapshot_id)

    def list_snapshots(self, limit: int = 20) -> List[Dict]:
        sorted_snaps = sorted(self._snapshots.values(), key=lambda s: s.created_at, reverse=True)
        return [
            {
                "id": s.id,
                "label": s.label,
                "created_at": s.created_at,
                "size_bytes": s.size_bytes,
            }
            for s in sorted_snaps[:limit]
        ]

    def delete(self, snapshot_id: str) -> bool:
        if snapshot_id not in self._snapshots:
            return False
        del self._snapshots[snapshot_id]
        if self._persist_dir:
            snap_path = os.path.join(self._persist_dir, f"{snapshot_id}.json")
            if os.path.exists(snap_path):
                os.remove(snap_path)
        self._save_index()
        return True

    def get_latest(self) -> Optional[Snapshot]:
        if not self._snapshots:
            return None
        return max(self._snapshots.values(), key=lambda s: s.created_at)

    def get_stats(self) -> Dict:
        total = len(self._snapshots)
        total_size = sum(s.size_bytes for s in self._snapshots.values())
        return {
            "total_snapshots": total,
            "total_size_bytes": total_size,
            "total_size_kb": round(total_size / 1024, 1),
            "latest": self.get_latest().id if self._snapshots else None,
        }
