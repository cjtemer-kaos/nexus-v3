# src/core/pointer_store.py
"""
Pointer Pattern — Large outputs to disk.

When an agent output exceeds threshold (default 30KB), save to disk
and replace with a pointer in the conversation. This prevents memory bloat
and context window pollution.

Usage:
    store = PointerStore()
    pointer = store.maybe_store(large_output, source="agent-coder")
    if pointer:
        # Use pointer.placeholder() in conversation instead of raw content
        conversation.append({"role": "assistant", "content": pointer.placeholder()})
    # Later, retrieve full content:
    full = store.retrieve(pointer.id)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from src.security.atomic_io import atomic_write_json, atomic_write_text

logger = logging.getLogger(__name__)

DEFAULT_STORAGE = Path.home() / ".nexus" / "pointers"
DEFAULT_THRESHOLD = 30 * 1024  # 30KB


@dataclass
class Pointer:
    id: str
    path: str
    size_bytes: int
    source: str
    created_at: float
    content_hash: str = ""

    def placeholder(self) -> str:
        """Placeholder string for conversation context."""
        return (
            f"[POINTER:{self.id}] Output stored to disk "
            f"({self.size_bytes} bytes from {self.source}). "
            f"Use pointer_store.retrieve('{self.id}') to access full content."
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "source": self.source,
            "created_at": self.created_at,
        }


class PointerStore:
    """Stores large outputs to disk, returns lightweight pointers."""

    def __init__(
        self,
        storage_dir: str | None = None,
        threshold_bytes: int = DEFAULT_THRESHOLD,
        max_age_hours: float = 72.0,
    ):
        self._dir = Path(storage_dir or str(DEFAULT_STORAGE))
        self._dir.mkdir(parents=True, exist_ok=True)
        self._threshold = threshold_bytes
        self._max_age_hours = max_age_hours
        self._pointers: dict[str, Pointer] = {}
        self._load_index()

    def maybe_store(self, content: str, source: str = "") -> Pointer | None:
        """Store content if it exceeds threshold. Returns Pointer or None."""
        size = len(content.encode("utf-8", errors="replace"))
        if size < self._threshold:
            return None

        content_hash = hashlib.md5(content[:1000].encode()).hexdigest()[:8]
        ptr_id = f"ptr_{int(time.time())}_{content_hash}"
        path = str(self._dir / f"{ptr_id}.txt")

        atomic_write_text(path, content)

        pointer = Pointer(
            id=ptr_id,
            path=path,
            size_bytes=size,
            source=source,
            created_at=time.time(),
            content_hash=content_hash,
        )
        self._pointers[ptr_id] = pointer
        self._save_index()
        logger.info(f"PointerStore: saved {size} bytes from {source} as {ptr_id}")
        return pointer

    def retrieve(self, ptr_id: str) -> str | None:
        """Retrieve full content by pointer ID."""
        pointer = self._pointers.get(ptr_id)
        if not pointer:
            return None
        try:
            with open(pointer.path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            logger.warning(f"PointerStore: file not found for {ptr_id}")
            return None

    def list_pointers(self) -> list[Pointer]:
        return list(self._pointers.values())

    def cleanup(self, max_age_hours: float | None = None) -> int:
        """Remove pointers older than max_age_hours. Returns count removed."""
        max_age = max_age_hours if max_age_hours is not None else self._max_age_hours
        cutoff = time.time() - (max_age * 3600)
        removed = 0
        to_remove = []
        for ptr_id, ptr in self._pointers.items():
            if ptr.created_at < cutoff:
                try:
                    os.unlink(ptr.path)
                except OSError:
                    pass
                to_remove.append(ptr_id)
                removed += 1
        for ptr_id in to_remove:
            del self._pointers[ptr_id]
        if removed:
            self._save_index()
            logger.info(f"PointerStore: cleaned up {removed} pointers")
        return removed

    def status(self) -> dict:
        total_bytes = sum(p.size_bytes for p in self._pointers.values())
        return {
            "total_pointers": len(self._pointers),
            "total_bytes": total_bytes,
            "storage_dir": str(self._dir),
            "threshold_bytes": self._threshold,
        }

    def _save_index(self) -> None:
        index_path = self._dir / "index.json"
        data = {pid: p.to_dict() for pid, p in self._pointers.items()}
        atomic_write_json(index_path, data)

    def _load_index(self) -> None:
        index_path = self._dir / "index.json"
        if index_path.exists():
            try:
                with open(index_path, "r") as f:
                    data = json.load(f)
                for pid, d in data.items():
                    self._pointers[pid] = Pointer(**d)
            except (json.JSONDecodeError, TypeError):
                pass
