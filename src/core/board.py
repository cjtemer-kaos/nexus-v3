"""Board de tareas con claims (estilo NexusHive) — sqlite simple, sin deps.

Estados: open | claimed | done | failed | blocked | cancelled.
Gates: una tarea con deps sin `done` no se puede reclamar (fail-closed).
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

_TERMINAL = {"done", "failed", "cancelled"}


class Board:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks("
            "id TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT,"
            "assignee TEXT, created_by TEXT, priority INTEGER, deps TEXT,"
            "created_at REAL, updated_at REAL, notes TEXT)")
        self._conn.commit()

    def _row(self, r) -> dict:
        return {"id": r[0], "title": r[1], "description": r[2], "status": r[3],
                "assignee": r[4], "created_by": r[5], "priority": r[6],
                "deps": [d for d in (r[7] or "").split(",") if d],
                "created_at": r[8], "updated_at": r[9], "notes": r[10]}

    def get(self, tid: str) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT id,title,description,status,assignee,created_by,priority,deps,"
                "created_at,updated_at,notes FROM tasks WHERE id=?", (tid,)).fetchone()
        return self._row(r) if r else None

    def list(self, status: str = "", assignee: str = "") -> list[dict]:
        q = "SELECT id,title,description,status,assignee,created_by,priority,deps,created_at,updated_at,notes FROM tasks"
        where, args = [], []
        if status:
            where.append("status=?"); args.append(status)
        if assignee:
            where.append("assignee=?"); args.append(assignee)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY priority DESC, created_at ASC"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._row(r) for r in rows]

    def create(self, title: str, description: str = "", assignee: str = "",
               created_by: str = "", priority: int = 3, deps: str = "") -> dict:
        tid = uuid.uuid4().hex[:10]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks(id,title,description,status,assignee,created_by,priority,deps,"
                "created_at,updated_at,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (tid, title, description, "open", assignee, created_by, int(priority), deps, now, now, ""))
            self._conn.commit()
        return self.get(tid)  # type: ignore[return-value]

    def _deps_done(self, deps: list[str]) -> bool:
        for d in deps:
            t = self.get(d)
            if t is None or t["status"] != "done":
                return False
        return True

    def claim(self, tid: str, agent: str) -> dict:
        t = self.get(tid)
        if t is None:
            return {"ok": False, "error": "no existe"}
        if t["status"] in _TERMINAL:
            return {"ok": False, "error": f"estado terminal: {t['status']}"}
        if t["status"] == "claimed" and t["assignee"] != agent:
            return {"ok": False, "error": f"ya reclamada por {t['assignee']}"}
        if not self._deps_done(t["deps"]):
            return {"ok": False, "error": "deps sin completar (gate)"}
        with self._lock:
            self._conn.execute("UPDATE tasks SET status='claimed', assignee=?, updated_at=? WHERE id=?",
                               (agent, time.time(), tid))
            self._conn.commit()
        return {"ok": True, "task": self.get(tid)}

    def update(self, tid: str, status: str = "", notes: str = "", assignee: str = "") -> dict:
        t = self.get(tid)
        if t is None:
            return {"ok": False, "error": "no existe"}
        sets, args = ["updated_at=?"], [time.time()]
        if status:
            sets.append("status=?"); args.append(status)
        if assignee:
            sets.append("assignee=?"); args.append(assignee)
        if notes:
            sets.append("notes=?"); args.append((t["notes"] + "\n" + notes).strip())
        args.append(tid)
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", args)
            self._conn.commit()
        return {"ok": True, "task": self.get(tid)}


_BOARDS: dict[str, Board] = {}
_BOARDS_LOCK = threading.Lock()


def get_board(path: str | Path) -> Board:
    key = str(path)
    with _BOARDS_LOCK:
        b = _BOARDS.get(key)
        if b is None:
            b = Board(key)
            _BOARDS[key] = b
        return b
