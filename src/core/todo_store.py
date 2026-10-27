"""Todo store — last-write-wins task list, port of @deepseek-ai/dsh-tool-todo.

Spec verbatim: whole-list replacement on every todo_write, 3 statuses
(pending|in_progress|completed), allowParallel configurable. Stored per-session
in SQLite so resume/fork sees latest snapshot. Projection semantics: last-write-wins.

Harness uses cordis sessionProjections; here we use the existing session.db
with a tiny todos table (no cross-project import — copied behavior, isolated).
"""
from __future__ import annotations
import json, sqlite3, threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List

VALID_STATUSES = ("pending", "in_progress", "completed")

@dataclass
class TodoItem:
    content: str
    status: str  # pending | in_progress | completed

FORMA = 'lista de objetos [{"content": "...", "status": "pending|in_progress|completed"}]'


def _validate(todos: List[dict], allow_parallel: bool) -> List[TodoItem]:
    # El registry coerciona `todos: "x"` a `["x"]` (los modelos chicos mandan un
    # string suelto), asi que aca puede llegar una lista de strings. Validar la
    # forma con un mensaje accionable evita el AttributeError del `.get`.
    if isinstance(todos, (str, bytes)) or not isinstance(todos, (list, tuple)):
        raise ValueError(f"todos invalido: se esperaba {FORMA} (recibido {type(todos).__name__})")
    seen = set()
    active = 0
    out: List[TodoItem] = []
    for i, item in enumerate(todos):
        if not isinstance(item, dict):
            raise ValueError(
                f"todos[{i}] invalido: se esperaba un objeto con content y status, "
                f"no {type(item).__name__}. Forma esperada: {FORMA}")
        raw_content = item.get("content")
        if raw_content is None:
            raw_content = ""
        if not isinstance(raw_content, str):
            raise ValueError(
                f"todos[{i}].content invalido: debe ser string (recibido {type(raw_content).__name__})")
        content = raw_content.strip()
        raw_status = item.get("status")
        if raw_status is None:
            raw_status = ""
        if not isinstance(raw_status, str):
            raise ValueError(
                f"todos[{i}].status invalido: debe ser string (recibido {type(raw_status).__name__})")
        status = raw_status.strip()
        if not content:
            raise ValueError(f"todos[{i}].content invalido: no puede estar vacio")
        if status not in VALID_STATUSES:
            raise ValueError(
                f"todos[{i}].status invalido: {status!r}. Valores validos: {list(VALID_STATUSES)}")
        if content in seen:
            raise ValueError(f"todos duplicados: {content!r} aparece mas de una vez")
        seen.add(content)
        if status == "in_progress":
            active += 1
        out.append(TodoItem(content=content, status=status))
    if not allow_parallel and active > 1:
        raise ValueError(f"invalid todos: at most one in_progress (got {active})")
    return out

_SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
  session_id TEXT PRIMARY KEY,
  todos_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

class TodoStore:
    """Per-session last-write-wins todo list."""
    def __init__(self, db_path: Path | str, allow_parallel: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.allow_parallel = allow_parallel
        self._lock = threading.RLock()
        self._init()

    def _init(self):
        with sqlite3.connect(str(self.db_path)) as c:
            c.executescript(_SCHEMA)

    def write(self, session_id: str, todos: List[dict]) -> dict:
        items = _validate(todos, self.allow_parallel)
        payload = json.dumps([asdict(i) for i in items], ensure_ascii=False)
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as c:
                c.execute("INSERT INTO todos(session_id, todos_json, updated_at) VALUES(?,?,datetime('now')) ON CONFLICT(session_id) DO UPDATE SET todos_json=excluded.todos_json, updated_at=datetime('now')", (session_id, payload))
        return self.read(session_id)

    def read(self, session_id: str) -> dict:
        with sqlite3.connect(str(self.db_path)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT todos_json FROM todos WHERE session_id=?", (session_id,)).fetchone()
            raw = json.loads(row["todos_json"]) if row else []
        counts = {"pending": 0, "inProgress": 0, "completed": 0}
        for t in raw:
            s = t.get("status")
            if s == "pending": counts["pending"] += 1
            elif s == "in_progress": counts["inProgress"] += 1
            elif s == "completed": counts["completed"] += 1
        return {"todos": raw, "counts": counts}

    def clear(self, session_id: str):
        with sqlite3.connect(str(self.db_path)) as c:
            c.execute("DELETE FROM todos WHERE session_id=?", (session_id,))
