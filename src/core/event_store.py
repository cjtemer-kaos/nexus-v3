"""Event Store — append-only event sourcing for replay/audit.

Inspired by SuperNEXUS v2 src/core/event_store.py + Hermes trajectory.
Ver BLUEPRINT.md §5.2 (Event sourcing opcional).

WAL + indexes. Thread-safe. Single SQLite file: data/event_store.db
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

logger = logging.getLogger("nexus-event-store")


class EventKind:
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    MEMORY_WRITE = "memory_write"
    STATE_CHANGE = "state_change"
    HANDOFF = "handoff"
    COMPACT = "compact"
    FORK = "fork"
    CUSTOM = "custom"


@dataclass
class Event:
    kind: str
    data: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:32])
    conversation_id: str = ""  # == session_id in v3
    session_id: str = ""  # alias, synced with conversation_id
    timestamp: float = field(default_factory=time.time)
    parent_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # keep conversation_id and session_id in sync
        if self.session_id and not self.conversation_id:
            self.conversation_id = self.session_id
        if self.conversation_id and not self.session_id:
            self.session_id = self.conversation_id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "data": self.data,
            "timestamp": self.timestamp,
            "parent_id": self.parent_id,
            "metadata": self.metadata,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    conversation_id TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    data TEXT DEFAULT '{}',
    timestamp REAL DEFAULT (strftime('%s', 'now')),
    parent_id TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conversation_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_id);
"""


class EventStore:
    """Append-only event log. Thread-safe. WAL."""

    def __init__(self, db_path: str | Path | None = None, wal: bool = True):
        if db_path is None:
            # default relative to project root (two levels up from src/core)
            default = Path(__file__).parents[2] / "data" / "event_store.db"
            db_path = str(default)
        self.db_path = Path(db_path) if str(db_path) != ":memory:" else Path(":memory:")
        self._db_str = str(db_path)
        self._wal = wal
        self._lock = threading.RLock()
        self._callbacks: Dict[str, List[Callable]] = {}
        if self._db_str != ":memory:":
            Path(self._db_str).parent.mkdir(parents=True, exist_ok=True)
        # For :memory: keep single shared connection (otherwise each connect is blank DB)
        self._mem_conn: sqlite3.Connection | None = None
        self._in_memory = self._db_str == ":memory:"
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._in_memory:
            if self._mem_conn is None:
                self._mem_conn = sqlite3.connect(self._db_str, check_same_thread=False, isolation_level=None, timeout=30.0)
                self._mem_conn.execute("PRAGMA busy_timeout=5000;")
            return self._mem_conn
        conn = sqlite3.connect(self._db_str, check_same_thread=False, isolation_level=None, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _release_conn(self, conn: sqlite3.Connection) -> None:
        if not self._in_memory:
            try:
                conn.close()
            except Exception:
                pass

    def _init_db(self) -> None:
        conn = self._get_conn()
        try:
            if self._wal and not self._in_memory:
                try:
                    conn.execute("PRAGMA journal_mode=WAL;")
                except Exception:
                    pass
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.executescript(_SCHEMA)
            # migrate: ensure session_id column exists for old DBs
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
                if "session_id" not in cols:
                    conn.execute("ALTER TABLE events ADD COLUMN session_id TEXT DEFAULT ''")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id)")
            except Exception:
                pass
        finally:
            self._release_conn(conn)

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------
    def append(self, event: Event) -> str:
        # sync ids
        if event.session_id and not event.conversation_id:
            event.conversation_id = event.session_id
        if event.conversation_id and not event.session_id:
            event.session_id = event.conversation_id
        conn = self._get_conn()
        try:
            with self._lock:
                conn.execute(
                    "INSERT INTO events (id, kind, conversation_id, session_id, data, timestamp, parent_id, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.id,
                        event.kind,
                        event.conversation_id,
                        event.session_id,
                        json.dumps(event.data, ensure_ascii=False),
                        event.timestamp,
                        event.parent_id,
                        json.dumps(event.metadata, ensure_ascii=False),
                    ),
                )
        finally:
            self._release_conn(conn)
        self._trigger_callbacks(event.kind, event)
        return event.id

    def append_event(
        self,
        kind: str,
        data: dict | None = None,
        conversation_id: str = "",
        session_id: str = "",
        parent_id: str = "",
        metadata: dict | None = None,
    ) -> str:
        cid = session_id or conversation_id
        ev = Event(
            kind=kind,
            data=data or {},
            conversation_id=cid,
            session_id=cid,
            parent_id=parent_id,
            metadata=metadata or {},
        )
        return self.append(ev)

    # ------------------------------------------------------------------
    # Replay / query
    # ------------------------------------------------------------------
    def replay(self, conversation_id: str, since: float = 0, limit: int | None = None) -> List[Event]:
        return self.get_events(conversation_id=conversation_id, since=since, limit=limit)

    def get_events(
        self,
        conversation_id: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
        since: float = 0,
        limit: int | None = None,
    ) -> List[Event]:
        cid = session_id or conversation_id
        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            q = "SELECT * FROM events WHERE timestamp > ?"
            params: list[Any] = [since]
            if cid:
                q += " AND (conversation_id=? OR session_id=?)"
                params.extend([cid, cid])
            if kind:
                q += " AND kind=?"
                params.append(kind)
            q += " ORDER BY timestamp ASC"
            if limit is not None:
                q += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(q, params).fetchall()
            events: List[Event] = []
            for r in rows:
                try:
                    data = json.loads(r["data"]) if r["data"] else {}
                except Exception:
                    data = {}
                try:
                    meta = json.loads(r["metadata"]) if r["metadata"] else {}
                except Exception:
                    meta = {}
                events.append(
                    Event(
                        id=r["id"],
                        kind=r["kind"],
                        conversation_id=r["conversation_id"] or r["session_id"] or "",
                        session_id=r["session_id"] or r["conversation_id"] or "",
                        data=data,
                        timestamp=r["timestamp"],
                        parent_id=r["parent_id"] or "",
                        metadata=meta,
                    )
                )
            return events
        finally:
            try:
                conn.row_factory = None  # type: ignore
            except Exception:
                pass
            self._release_conn(conn)

    def list_events(self, kind: str | None = None, limit: int = 100, offset: int = 0) -> List[Event]:
        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            if kind:
                rows = conn.execute(
                    "SELECT * FROM events WHERE kind=? ORDER BY timestamp DESC LIMIT ? OFFSET ?", (kind, limit, offset)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM events ORDER BY timestamp DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            events: List[Event] = []
            for r in rows:
                try:
                    data = json.loads(r["data"]) if r["data"] else {}
                except Exception:
                    data = {}
                try:
                    meta = json.loads(r["metadata"]) if r["metadata"] else {}
                except Exception:
                    meta = {}
                events.append(
                    Event(
                        id=r["id"],
                        kind=r["kind"],
                        conversation_id=r["conversation_id"] or "",
                        session_id=r["session_id"] or r["conversation_id"] or "",
                        data=data,
                        timestamp=r["timestamp"],
                        parent_id=r["parent_id"] or "",
                        metadata=meta,
                    )
                )
            return events
        finally:
            try:
                conn.row_factory = None  # type: ignore
            except Exception:
                pass
            self._release_conn(conn)

    def count(self, conversation_id: str | None = None, kind: str | None = None) -> int:
        conn = self._get_conn()
        try:
            q = "SELECT COUNT(*) FROM events WHERE 1=1"
            params: list[Any] = []
            if conversation_id:
                q += " AND (conversation_id=? OR session_id=?)"
                params.extend([conversation_id, conversation_id])
            if kind:
                q += " AND kind=?"
                params.append(kind)
            row = conn.execute(q, params).fetchone()
            return row[0] if row else 0
        finally:
            self._release_conn(conn)

    # ------------------------------------------------------------------
    # Pub/sub
    # ------------------------------------------------------------------
    def subscribe(self, kind: str, callback: Callable) -> None:
        with self._lock:
            self._callbacks.setdefault(kind, []).append(callback)

    def unsubscribe(self, kind: str, callback: Callable) -> None:
        with self._lock:
            if kind in self._callbacks:
                try:
                    self._callbacks[kind].remove(callback)
                except ValueError:
                    pass

    def _trigger_callbacks(self, kind: str, event: Event) -> None:
        cbs = list(self._callbacks.get(kind, [])) + list(self._callbacks.get("*", []))
        for cb in cbs:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Event callback error for {kind}: {e}")

    # ------------------------------------------------------------------
    # Stats / maintenance
    # ------------------------------------------------------------------
    def get_stats(self) -> dict:
        conn = self._get_conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            by_kind = conn.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind").fetchall()
            try:
                journal = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            except Exception:
                journal = "unknown"
            return {
                "total_events": total,
                "by_kind": {k: c for k, c in by_kind},
                "subscribers": {k: len(cbs) for k, cbs in self._callbacks.items()},
                "journal_mode": journal,
                "wal": self._wal,
                "db_path": self._db_str,
            }
        finally:
            self._release_conn(conn)

    def check_wal(self) -> str:
        conn = self._get_conn()
        try:
            row = conn.execute("PRAGMA journal_mode;").fetchone()
            return row[0] if row else "unknown"
        finally:
            self._release_conn(conn)

    def close(self) -> None:
        with self._lock:
            if self._mem_conn is not None:
                try:
                    self._mem_conn.close()
                except Exception:
                    pass
                self._mem_conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
