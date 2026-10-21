"""Session Log — WAL + FTS5 (DeepSeek session + SuperNEXUS session_manager + Hermes trajectory).

Ver BLUEPRINT.md §5.
Single SQLite file: data/session.db with WAL + FTS5 + branching.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Message:
    role: str  # system|user|assistant|tool
    content: str
    tool_call_id: str = ""
    tool_name: str = ""
    tokens: int = 0
    model: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tokens": self.tokens,
            "model": self.model,
            "created_at": self.created_at,
        }


@dataclass
class Session:
    id: str
    project: str = "default"
    parent_id: Optional[str] = None  # maps to parent_session_id column (BLUEPRINT compat)
    summary: str = ""
    compact_count: int = 0
    total_tokens: int = 0
    # Id del mensaje `system` que contiene el ultimo resumen de compactacion.
    # Sobrevive al reinicio: el historial se recarga desde aca (no se re-resume).
    summary_message_id: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_active: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project": self.project,
            "parent_id": self.parent_id,
            "parent_session_id": self.parent_id,
            "summary": self.summary,
            "compact_count": self.compact_count,
            "total_tokens": self.total_tokens,
            "summary_message_id": self.summary_message_id,
            "created_at": self.created_at,
            "last_active": self.last_active,
        }


# ---------------------------------------------------------------------------
# Schema — BLUEPRINT §5.3 compatible
# Uses parent_session_id as canonical column, with parent_id alias via view compat
# We use parent_session_id in DDL to match blueprint exactly, but support both names.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL DEFAULT 'default',
  parent_session_id TEXT REFERENCES sessions(id),
  created_at TEXT NOT NULL,
  last_active TEXT NOT NULL,
  summary TEXT DEFAULT '',
  compact_count INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  summary_message_id INTEGER
);
-- alias for legacy code using parent_id
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  tool_call_id TEXT DEFAULT '',
  tool_name TEXT DEFAULT '',
  tokens INTEGER DEFAULT 0,
  model TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role);

CREATE TABLE IF NOT EXISTS compaction_state (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  updated_at TEXT NOT NULL,
  tokens_before INTEGER DEFAULT 0,
  tokens_after INTEGER DEFAULT 0,
  saved INTEGER DEFAULT 0,
  layers TEXT DEFAULT '[]',
  actions INTEGER DEFAULT 0,
  summary_message_id INTEGER,
  summary TEXT DEFAULT '',
  count INTEGER DEFAULT 1
);

CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5(
  content,
  content='messages',
  content_rowid='id',
  tokenize='porter unicode61'
);
-- FTS triggers (INSERT + DELETE + UPDATE)
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO session_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO session_fts(session_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO session_fts(session_fts, rowid, content) VALUES ('delete', old.id, old.content);
  INSERT INTO session_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class SessionLog:
    """WAL + FTS5 session log. Thread-safe. Single SQLite file.

    Features:
    - WAL journal_mode for concurrent readers
    - FTS5 full-text search over message content
    - Hierarchical sessions via parent_session_id (branching / fork)
    - Thread-safe via RLock + check_same_thread=False + busy_timeout
    """

    def __init__(self, db_path: str | Path = "./data/session.db", wal: bool = True, fts: bool = True):
        self.db_path = Path(db_path)
        # :memory: is special — no parent mkdir needed
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._wal = wal
        self._fts = fts
        self._lock = threading.RLock()
        # isolation_level=None => autocommit; busy_timeout for WAL concurrency
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None, timeout=30.0)
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        if wal and str(self.db_path) != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode=WAL;")
            except Exception:
                pass
        self._init_schema()
        self._in_memory = str(self.db_path) == ":memory:"

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Migracion: DBs creadas antes del resumen durable no tienen la columna.
            try:
                _cols = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
                if "summary_message_id" not in _cols:
                    self._conn.execute("ALTER TABLE sessions ADD COLUMN summary_message_id INTEGER")
            except Exception:
                pass
            # Backfill FTS una vez (marcador user_version): en una tabla FTS5
            # external-content, count(*) devuelve el count de la tabla de contenido,
            # no del indice -> no sirve para detectar desincronizacion.
            try:
                uv = self._conn.execute("PRAGMA user_version").fetchone()[0]
                if uv < 1:
                    self._conn.execute("INSERT INTO session_fts(session_fts) VALUES ('rebuild')")
                    self._conn.execute("PRAGMA user_version = 1")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _row_to_session(self, row: sqlite3.Row | tuple) -> Session:
        # row: id, project, parent_session_id, created_at, last_active, summary, compact_count, total_tokens
        if isinstance(row, sqlite3.Row):
            return Session(
                id=row["id"],
                project=row["project"],
                parent_id=row["parent_session_id"],
                created_at=row["created_at"],
                last_active=row["last_active"],
                summary=row["summary"] or "",
                compact_count=row["compact_count"] or 0,
                total_tokens=row["total_tokens"] or 0,
                summary_message_id=(row["summary_message_id"]
                                    if "summary_message_id" in row.keys() else None),
            )
        # tuple fallback
        return Session(
            id=row[0], project=row[1], parent_id=row[2],
            created_at=row[3], last_active=row[4],
            summary=row[5] or "", compact_count=row[6] or 0, total_tokens=row[7] or 0,
            summary_message_id=(row[8] if len(row) > 8 else None),
        )

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------
    def create_session(self, project: str = "default", parent_id: str | None = None, session_id: str | None = None) -> Session:
        """Crea una sesión. Si `session_id` se provee, se usa ese id (para que los
        ids que manda el cliente sean estables entre turnos => memoria multi-turno)."""
        s = Session(id=session_id or uuid.uuid4().hex[:12], project=project, parent_id=parent_id)
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(id, project, parent_session_id, created_at, last_active, summary, compact_count, total_tokens) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (s.id, s.project, s.parent_id, s.created_at, s.last_active, s.summary, s.compact_count, s.total_tokens),
            )
        return s

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            self._conn.row_factory = None
        if row is None:
            return None
        return self._row_to_session(row)

    def list_sessions(self, project: str | None = None, limit: int = 100) -> list[Session]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            if project:
                rows = self._conn.execute(
                    "SELECT * FROM sessions WHERE project=? ORDER BY last_active DESC LIMIT ?", (project, limit)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM sessions ORDER BY last_active DESC LIMIT ?", (limit,)).fetchall()
            self._conn.row_factory = None
        return [self._row_to_session(r) for r in rows]

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            # Guard FK: si tiene hijos (fork/branch) la FK hace raise; borrá los hijos primero.
            cur = self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            # cascade deletes messages via FK
            return cur.rowcount > 0

    def update_session(self, session_id: str, **fields) -> bool:
        """Generic update: summary, project, etc."""
        allowed = {"summary", "project", "compact_count", "total_tokens", "summary_message_id"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return False
        vals.append(session_id)
        with self._lock:
            cur = self._conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", vals)
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def append(self, session_id: str, msg: Message) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(session_id, role, content, tool_call_id, tool_name, tokens, model, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (session_id, msg.role, msg.content, msg.tool_call_id, msg.tool_name, msg.tokens, msg.model, msg.created_at),
            )
            self._conn.execute(
                "UPDATE sessions SET last_active=?, total_tokens=total_tokens+? WHERE id=?",
                (msg.created_at, msg.tokens, session_id),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_messages(self, session_id: str, limit: int = 100, offset: int = 0) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, tool_call_id, tool_name, tokens, model, created_at FROM messages"
                " WHERE session_id=? ORDER BY id ASC LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ).fetchall()
        return [Message(*r) for r in rows]

    def messages_with_ids(self, session_id: str, limit: int = 10000) -> list[dict]:
        """Mensajes con id (para UI de rewind/edición). Shape completo."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, tool_call_id, tool_name, tokens, model, created_at"
                " FROM messages WHERE session_id=? ORDER BY id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [{"id": r[0], "role": r[1], "content": r[2], "tool_call_id": r[3],
                 "tool_name": r[4], "tokens": r[5], "model": r[6], "created_at": r[7]} for r in rows]

    def rewind(self, session_id: str, message_id: int) -> int:
        """Borra los mensajes posteriores a message_id (exclusive). Devuelve cuántos borró."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE session_id=? AND id > ?", (session_id, int(message_id)))
            self._conn.commit()
            return cur.rowcount or 0

    def usage(self, session_id: str = "") -> dict:
        """Agrega mensajes/tokens por modelo (opcional por sesión)."""
        where = " WHERE session_id=?" if session_id else ""
        args = [session_id] if session_id else []
        with self._lock:
            rows = self._conn.execute(
                f"SELECT model, COUNT(*), COALESCE(SUM(tokens),0) FROM messages{where} GROUP BY model",
                args).fetchall()
            tot = self._conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(tokens),0) FROM messages{where}", args).fetchone()
        by_model = [{"model": r[0] or "?", "messages": int(r[1]), "tokens": int(r[2] or 0)} for r in rows]
        return {"session_id": session_id or None, "messages": int(tot[0] or 0),
                "tokens": int(tot[1] or 0), "by_model": by_model}

    def count_messages(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)).fetchone()
        return row[0] if row else 0

    def count_messages_all(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return row[0] if row else 0

    def get_recent_messages(self, session_id: str, n: int = 10) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, tool_call_id, tool_name, tokens, model, created_at FROM messages"
                " WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
        # reverse to ASC order
        return [Message(*r) for r in reversed(rows)]

    # ------------------------------------------------------------------
    # FTS search
    # ------------------------------------------------------------------
    def search(self, query: str, limit: int = 20) -> list[Message]:
        """FTS5 search across all sessions. Uses porter stemming."""
        if not query or not query.strip():
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT m.role, m.content, m.tool_call_id, m.tool_name, m.tokens, m.model, m.created_at"
                    " FROM session_fts JOIN messages m ON m.id=session_fts.rowid"
                    " WHERE session_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # FTS syntax error fallback to LIKE (escapando %/_ para no tratarlos como comodines)
                _like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                rows = self._conn.execute(
                    "SELECT role, content, tool_call_id, tool_name, tokens, model, created_at FROM messages"
                    " WHERE content LIKE ? ESCAPE '\\' LIMIT ?",
                    (_like, limit),
                ).fetchall()
        return [Message(*r) for r in rows]

    def search_hits(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 global con id y session_id (para que la UI pueda abrir el resultado)."""
        if not query or not query.strip():
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT m.id, m.session_id, m.role, m.content, m.created_at"
                    " FROM session_fts JOIN messages m ON m.id=session_fts.rowid"
                    " WHERE session_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                _like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                rows = self._conn.execute(
                    "SELECT id, session_id, role, content, created_at FROM messages"
                    " WHERE content LIKE ? ESCAPE '\\' LIMIT ?",
                    (_like, limit),
                ).fetchall()
        return [{"id": r[0], "session_id": r[1], "role": r[2], "content": r[3],
                 "created_at": str(r[4] or "")} for r in rows]

    def search_session(self, session_id: str, query: str, limit: int = 20) -> list[Message]:
        """FTS search filtered to a single session."""
        if not query or not query.strip():
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT m.role, m.content, m.tool_call_id, m.tool_name, m.tokens, m.model, m.created_at"
                    " FROM session_fts JOIN messages m ON m.id=session_fts.rowid"
                    " WHERE session_fts MATCH ? AND m.session_id=? ORDER BY rank LIMIT ?",
                    (query, session_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                _like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                rows = self._conn.execute(
                    "SELECT role, content, tool_call_id, tool_name, tokens, model, created_at FROM messages"
                    " WHERE session_id=? AND content LIKE ? ESCAPE '\\' LIMIT ?",
                    (session_id, _like, limit),
                ).fetchall()
        return [Message(*r) for r in rows]

    # ------------------------------------------------------------------
    # Compaction + branching
    # ------------------------------------------------------------------
    def compact_mark(self, session_id: str, summary: str) -> Optional[int]:
        """Persiste el resumen como mensaje real y apunta `summary_message_id`.

        Antes el resumen vivia solo en el campo `summary` y el historial se
        recargaba sin el: al reiniciar, la compactacion se perdia. Ahora el
        resumen es un mensaje `system` con id estable (patron OpenCode
        `SummaryMessageID`), y el historial se recarga desde ahi.
        """
        content = (summary or "").strip()
        if not content.startswith("[resumen de"):
            content = f"[resumen de contexto]\n{content}".strip()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(session_id, role, content, tool_call_id, tool_name, tokens, model, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (session_id, "system", content, "", "", 0, "", datetime.now(timezone.utc).isoformat()),
            )
            mid = cur.lastrowid
            self._conn.execute(
                "UPDATE sessions SET summary=?, summary_message_id=?, compact_count=compact_count+1,"
                " last_active=? WHERE id=?",
                (summary, mid, datetime.now(timezone.utc).isoformat(), session_id),
            )
            return mid

    def get_summary_message(self, session_id: str) -> Optional[dict]:
        """Devuelve el mensaje de resumen vigente (o None)."""
        sess = self.get_session(session_id)
        if sess is None or not sess.summary_message_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, role, content, tool_call_id, tool_name, tokens, model, created_at"
                " FROM messages WHERE id=? AND session_id=?",
                (int(sess.summary_message_id), session_id),
            ).fetchone()
        if not row:
            return None
        return {"id": row[0], "role": row[1], "content": row[2], "tool_call_id": row[3],
                "tool_name": row[4], "tokens": row[5], "model": row[6], "created_at": row[7]}

    def save_compaction_state(self, session_id: str, *, tokens_before: int = 0,
                              tokens_after: int = 0, saved: int = 0,
                              layers: list[str] | None = None, actions: int = 0,
                              summary: str = "",
                              summary_message_id: int | None = None) -> dict:
        """Persiste el estado de la ultima compactacion (sobrevive reinicios).

        El resumen ya era durable como mensaje (`compact_mark`); esto agrega
        metricas y un fallback de resumen para cuando el mensaje no se puede
        recuperar (p.ej. sesion importada o mensaje purgado).
        """
        now = datetime.now(timezone.utc).isoformat()
        layers_json = json.dumps([str(x) for x in (layers or [])], ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO compaction_state(session_id, updated_at, tokens_before, tokens_after,"
                " saved, layers, actions, summary_message_id, summary, count)"
                " VALUES (?,?,?,?,?,?,?,?,?,1)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " updated_at=excluded.updated_at, tokens_before=excluded.tokens_before,"
                " tokens_after=excluded.tokens_after, saved=excluded.saved,"
                " layers=excluded.layers, actions=excluded.actions,"
                " summary_message_id=COALESCE(excluded.summary_message_id,"
                " compaction_state.summary_message_id),"
                " summary=CASE WHEN excluded.summary<>'' THEN excluded.summary"
                " ELSE compaction_state.summary END,"
                " count=compaction_state.count+1",
                (session_id, now, int(tokens_before), int(tokens_after), int(saved),
                 layers_json, int(actions), summary_message_id, summary or ""),
            )
        return self.get_compaction_state(session_id) or {}

    def get_compaction_state(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id, updated_at, tokens_before, tokens_after, saved,"
                " layers, actions, summary_message_id, summary, count"
                " FROM compaction_state WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        try:
            layers = json.loads(row[5] or "[]")
        except Exception:
            layers = []
        return {
            "session_id": row[0],
            "updated_at": row[1],
            "tokens_before": row[2],
            "tokens_after": row[3],
            "saved": row[4],
            "layers": layers if isinstance(layers, list) else [],
            "actions": row[6],
            "summary_message_id": row[7],
            "summary": row[8] or "",
            "count": row[9],
        }

    def get_messages_from(self, session_id: str, after_id: int = 0,
                          limit: int = 10000) -> list[dict]:
        """Mensajes con id > after_id (ASC), con shape completo."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, tool_call_id, tool_name, tokens, model, created_at"
                " FROM messages WHERE session_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (session_id, int(after_id), int(limit)),
            ).fetchall()
        return [{"id": r[0], "role": r[1], "content": r[2], "tool_call_id": r[3],
                 "tool_name": r[4], "tokens": r[5], "model": r[6], "created_at": r[7]} for r in rows]

    def fork(self, session_id: str) -> Session:
        """Branch: nueva sesion con parent_session_id = session_id (OpenCode multi-session)."""
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            row = self._conn.execute("SELECT project, summary, compact_count FROM sessions WHERE id=?", (session_id,)).fetchone()
            self._conn.row_factory = None
            project = row["project"] if row else "default"
            # inherit compact state
            parent_compact = row["compact_count"] if row else 0
            parent_summary = row["summary"] if row else ""
        child = self.create_session(project=project, parent_id=session_id)
        # Optionally carry summary/compact_count from parent
        if parent_summary or parent_compact:
            with self._lock:
                self._conn.execute(
                    "UPDATE sessions SET summary=?, compact_count=? WHERE id=?",
                    (parent_summary, parent_compact, child.id),
                )
                child.summary = parent_summary
                child.compact_count = parent_compact
        return child

    def branch(self, session_id: str, copy_messages: bool = False) -> Session:
        """Alias for fork. If copy_messages, replicate messages into child."""
        child = self.fork(session_id)
        if copy_messages:
            msgs = self.get_messages(session_id, limit=10000)
            for m in msgs:
                self.append(child.id, m)
        return child

    def tree(self, session_id: str) -> dict:
        """Arbol alrededor de una sesion (pi-style): cadena de ancestros + hijos."""
        with self._lock:
            rows = self._conn.execute("SELECT id, parent_session_id FROM sessions").fetchall()
        parent = {r[0]: r[1] for r in rows}
        children: dict[str, list[str]] = {}
        for sid, pid in parent.items():
            if pid:
                children.setdefault(pid, []).append(sid)
        chain: list[str] = []
        cur = parent.get(session_id)
        seen = set()
        while cur and cur not in seen:
            chain.append(cur)
            seen.add(cur)
            cur = parent.get(cur)
        chain.reverse()
        return {
            "ancestors": chain,
            "session": session_id,
            "children": children.get(session_id, []),
            "exists": session_id in parent,
        }

    # ------------------------------------------------------------------
    # Stats / maintenance
    # ------------------------------------------------------------------
    def get_stats(self) -> dict:
        with self._lock:
            sess_cnt = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            msg_cnt = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            try:
                fts_cnt = self._conn.execute("SELECT COUNT(*) FROM session_fts").fetchone()[0]
            except Exception:
                fts_cnt = 0
            try:
                journal = self._conn.execute("PRAGMA journal_mode;").fetchone()[0]
            except Exception:
                journal = "unknown"
        return {
            "sessions": sess_cnt,
            "messages": msg_cnt,
            "fts_rows": fts_cnt,
            "journal_mode": journal,
            "wal": self._wal,
            "fts": self._fts,
        }

    def check_wal(self) -> str:
        with self._lock:
            row = self._conn.execute("PRAGMA journal_mode;").fetchone()
        return row[0] if row else "unknown"

    def check_fts(self) -> bool:
        with self._lock:
            try:
                self._conn.execute("SELECT count(*) FROM session_fts LIMIT 1").fetchone()
                return True
            except Exception:
                return False

    def rebuild_fts(self) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO session_fts(session_fts) VALUES ('rebuild')")

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
