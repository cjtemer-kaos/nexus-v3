"""
Swarm Memory — structured event store for NexusHive.
Adapted from Hermes swarm-memory.ts.

Reemplaza el message board simple con eventos tipados,
checkpoints, handoffs y búsqueda estructurada.
"""

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class MemoryKind(str, Enum):
    PROFILE = "profile"
    MISSION = "mission"
    EPISODIC = "episodic"
    HANDOFF = "handoff"
    SHARED = "shared"


class EventType(str, Enum):
    MISSION_START = "mission-start"
    DISPATCH = "dispatch"
    CHECKPOINT = "checkpoint"
    HANDOFF_REQUESTED = "handoff-requested"
    HANDOFF_WRITTEN = "handoff-written"
    RESUME = "resume"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    NOTE = "note"


@dataclass
class MemoryEvent:
    at: float
    type: EventType
    worker_id: Optional[str] = None
    mission_id: Optional[str] = None
    assignment_id: Optional[str] = None
    summary: str = ""
    event_data: Optional[dict] = None


@dataclass
class MemoryFile:
    name: str
    path: str
    content: str


@dataclass
class SearchResult:
    path: str
    line: int
    score: float
    snippet: str


def validate_swarm_id(value: str) -> bool:
    return bool(re.match(r'^[a-z0-9][a-z0-9_-]{0,63}$', value, re.IGNORECASE))


def validate_mission_id(value: str) -> bool:
    return bool(re.match(r'^[a-z0-9][a-z0-9_.:-]{0,127}$', value, re.IGNORECASE))


class SwarmMemory:
    """
    Memoria estructurada para el swarm.
    Usa SQLite para eventos y filesystem para handoffs/artifacts.
    """

    def __init__(self, nexus_home: Optional[str] = None):
        home = nexus_home or os.environ.get("NEXUS_HOME", str(Path.home() / ".nexus"))
        self.root = Path(home) / "swarm"
        self.root.mkdir(parents=True, exist_ok=True)

        self.shared_root = self.root / "shared"
        self.handoff_root = self.root / "handoffs"
        for d in [self.shared_root, self.handoff_root]:
            d.mkdir(parents=True, exist_ok=True)

        self.db_path = self.root / "swarm.db"
        self._init_db()

        self.project_context_path = self.shared_root / "PROJECT.md"

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    type TEXT NOT NULL,
                    worker_id TEXT,
                    mission_id TEXT,
                    assignment_id TEXT,
                    summary TEXT DEFAULT '',
                    event_data TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
                CREATE INDEX IF NOT EXISTS idx_events_worker ON events(worker_id);
                CREATE INDEX IF NOT EXISTS idx_events_mission ON events(mission_id);
                CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);

                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    worker_id TEXT,
                    status TEXT DEFAULT 'in_progress',
                    summary TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    metadata TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_mission ON checkpoints(mission_id);

                CREATE TABLE IF NOT EXISTS handoffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_worker TEXT NOT NULL,
                    to_worker TEXT,
                    mission_id TEXT,
                    summary TEXT DEFAULT '',
                    context TEXT,
                    created_at REAL NOT NULL,
                    resolved_at REAL,
                    status TEXT DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS idx_handoffs_from ON handoffs(from_worker);
                CREATE INDEX IF NOT EXISTS idx_handoffs_status ON handoffs(status);
            """)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def append_event(self, event: MemoryEvent):
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO events (at, type, worker_id, mission_id, assignment_id, summary, event_data)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event.at, event.type.value, event.worker_id, event.mission_id,
                 event.assignment_id, event.summary,
                 json.dumps(event.event_data) if event.event_data else None)
            )
            conn.commit()
        finally:
            conn.close()

    def get_events(self, worker_id: Optional[str] = None, mission_id: Optional[str] = None,
                   limit: int = 50) -> List[dict]:
        conn = self._get_conn()
        try:
            parts = ["SELECT * FROM events WHERE 1=1"]
            params = []
            if worker_id:
                parts.append("AND worker_id = ?")
                params.append(worker_id)
            if mission_id:
                parts.append("AND mission_id = ?")
                params.append(mission_id)
            parts.append("ORDER BY at DESC LIMIT ?")
            params.append(limit)
            rows = conn.execute(" ".join(parts), params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    def set_checkpoint(self, mission_id: str, worker_id: str, status: str = "in_progress",
                       summary: str = "", metadata: Optional[dict] = None) -> int:
        now = time.time()
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT id FROM checkpoints WHERE mission_id = ? AND worker_id = ? ORDER BY id DESC LIMIT 1",
                (mission_id, worker_id)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE checkpoints SET status = ?, summary = ?, updated_at = ?, metadata = ? WHERE id = ?",
                    (status, summary, now, json.dumps(metadata) if metadata else None, existing["id"])
                )
                conn.commit()
                return existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO checkpoints (mission_id, worker_id, status, summary, created_at, updated_at, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (mission_id, worker_id, status, summary, now, now,
                     json.dumps(metadata) if metadata else None)
                )
                conn.commit()
                return cur.lastrowid
        finally:
            conn.close()

    def get_checkpoints(self, mission_id: Optional[str] = None, limit: int = 20) -> List[dict]:
        conn = self._get_conn()
        try:
            if mission_id:
                rows = conn.execute(
                    "SELECT * FROM checkpoints WHERE mission_id = ? ORDER BY updated_at DESC LIMIT ?",
                    (mission_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM checkpoints ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Handoffs
    # ------------------------------------------------------------------

    def create_handoff(self, from_worker: str, to_worker: Optional[str] = None,
                       mission_id: Optional[str] = None, summary: str = "",
                       context: Optional[dict] = None) -> int:
        now = time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO handoffs (from_worker, to_worker, mission_id, summary, context, created_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                (from_worker, to_worker, mission_id, summary,
                 json.dumps(context) if context else None, now)
            )
            conn.commit()
            self.append_event(MemoryEvent(
                at=now, type=EventType.HANDOFF_REQUESTED,
                worker_id=from_worker, mission_id=mission_id, summary=summary
            ))
            return cur.lastrowid
        finally:
            conn.close()

    def resolve_handoff(self, handoff_id: int, status: str = "resolved"):
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE handoffs SET status = ?, resolved_at = ? WHERE id = ?",
                (status, time.time(), handoff_id)
            )
            conn.commit()
        finally:
            conn.close()

    def get_pending_handoffs(self, worker_id: Optional[str] = None) -> List[dict]:
        conn = self._get_conn()
        try:
            if worker_id:
                rows = conn.execute(
                    "SELECT * FROM handoffs WHERE (to_worker = ? OR to_worker IS NULL) AND status = 'pending' ORDER BY created_at DESC",
                    (worker_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM handoffs WHERE status = 'pending' ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Shared context
    # ------------------------------------------------------------------

    def set_project_context(self, content: str):
        self.project_context_path.write_text(content, encoding="utf-8")

    def get_project_context(self) -> Optional[str]:
        if self.project_context_path.exists():
            return self.project_context_path.read_text(encoding="utf-8")
        return None

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        conn = self._get_conn()
        try:
            event_count = conn.execute("SELECT COUNT(*) as n FROM events").fetchone()["n"]
            checkpoint_count = conn.execute("SELECT COUNT(*) as n FROM checkpoints").fetchone()["n"]
            handoff_count = conn.execute("SELECT COUNT(*) as n FROM handoffs").fetchone()["n"]
            pending_handoffs = conn.execute(
                "SELECT COUNT(*) as n FROM handoffs WHERE status = 'pending'"
            ).fetchone()["n"]
            return {
                "events": event_count,
                "checkpoints": checkpoint_count,
                "handoffs": handoff_count,
                "pending_handoffs": pending_handoffs,
            }
        finally:
            conn.close()
