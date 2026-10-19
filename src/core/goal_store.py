"""Goal store — objetivo persistente por sesion (rondas autonomas / long-goal).

Estado orientado a objetivos: UN objetivo por sesion, last-write-wins.
Mismo patron que todo_store: tabla propia en session.db, sin tocar el esquema
de sessions/messages (portable, sin migraciones).
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

VALID_STATUSES = ("active", "done", "cleared")
MAX_OBJECTIVE = 4000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
  session_id TEXT PRIMARY KEY,
  objective TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class GoalStore:
    """Per-session persistent goal (last-write-wins)."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _init(self):
        with sqlite3.connect(str(self.db_path)) as c:
            c.executescript(_SCHEMA)

    def set(self, session_id: str, objective: str, status: str = "active") -> dict:
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id required")
        obj = str(objective or "").strip()
        if not obj:
            raise ValueError("objective must be a non-empty string")
        if len(obj) > MAX_OBJECTIVE:
            raise ValueError(f"objective too long (max {MAX_OBJECTIVE})")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status!r} — must be one of {VALID_STATUSES}")
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as c:
                c.execute(
                    "INSERT INTO goals(session_id, objective, status, updated_at)"
                    " VALUES(?,?,?,datetime('now'))"
                    " ON CONFLICT(session_id) DO UPDATE SET"
                    " objective=excluded.objective, status=excluded.status, updated_at=datetime('now')",
                    (sid, obj, status),
                )
        return self.get(sid)

    def get(self, session_id: str) -> dict:
        with sqlite3.connect(str(self.db_path)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT objective, status, updated_at FROM goals WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {"session_id": session_id, "exists": False, "objective": "", "status": "none"}
        return {
            "session_id": session_id,
            "exists": True,
            "objective": row["objective"],
            "status": row["status"],
            "updated_at": row["updated_at"],
        }

    def clear(self, session_id: str) -> bool:
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as c:
                cur = c.execute("DELETE FROM goals WHERE session_id=?", (session_id,))
                return cur.rowcount > 0
