"""Cola de aprobaciones + grants durables (HITL).

La cola sigue siendo la superficie que consume la UI (`/api/approvals`).
Los grants se persisten en SQLite para que un "siempre" sobreviva al reinicio
y para que un "deny" gane sobre cualquier allow (precedencia fail-closed).

Reglas:
- `once` se consume con el primer uso.
- `session` vale solo para la sesion que lo aprobo.
- `always` no tiene sesion (vale para todas).
- `deny` se evalua ANTES que `allow`: un deny no se puede vencer con un grant.
- Una clave vacia nunca cachea (evita aprobar "cualquier cosa" por accidente).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional


def fingerprint(tool: str, action: str, args: dict | None) -> str:
    """Hash estable de tool+action+args normalizados (clave de grant)."""
    clean = {
        k: v for k, v in (args or {}).items()
        if k not in ("confirm", "timeout", "_approval_id", "reason")
    }
    raw = json.dumps(
        {"tool": tool, "action": action, "args": clean},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class GrantStore:
    """Persistencia de grants. SQLite WAL, thread-safe, best-effort."""

    def __init__(self, db_path: str | Path = "./data/approvals.db") -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        try:
            if str(self.db_path) != ":memory:":
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False,
                isolation_level=None, timeout=10.0,
            )
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute("PRAGMA journal_mode=WAL;")
            # WAL + NORMAL: aprobaciones se escriben en cada tool gateada; con FULL cada
            # commit paga fsync (~28 ms en D:\ias, medido 2026-10-26).
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS approval_grants (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL DEFAULT '',
                  tool TEXT NOT NULL,
                  action TEXT NOT NULL DEFAULT '',
                  key_hash TEXT NOT NULL DEFAULT '',
                  decision TEXT NOT NULL,
                  scope TEXT NOT NULL,
                  uses_left INTEGER NOT NULL DEFAULT 1,
                  created_at REAL NOT NULL,
                  expires_at REAL,
                  note TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_grants_lookup
                  ON approval_grants(tool, action, session_id, decision);
                """
            )
        except Exception:
            # Sin disco (tests/import en directorio read-only): memoria.
            self._conn = sqlite3.connect(":memory:", check_same_thread=False,
                                         isolation_level=None)
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS approval_grants (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL DEFAULT '',
                  tool TEXT NOT NULL,
                  action TEXT NOT NULL DEFAULT '',
                  key_hash TEXT NOT NULL DEFAULT '',
                  decision TEXT NOT NULL,
                  scope TEXT NOT NULL,
                  uses_left INTEGER NOT NULL DEFAULT 1,
                  created_at REAL NOT NULL,
                  expires_at REAL,
                  note TEXT NOT NULL DEFAULT ''
                )"""
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def add(
        self,
        *,
        session_id: str,
        tool: str,
        action: str,
        key_hash: str,
        decision: str,
        scope: str,
        uses_left: int = 1,
        ttl_s: float | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        if decision not in ("allow", "deny"):
            raise ValueError(f"decision invalida: {decision}")
        if scope not in ("once", "session", "always"):
            raise ValueError(f"scope invalido: {scope}")
        if scope == "always":
            session_id = ""
        now = time.time()
        expires = now + ttl_s if ttl_s and ttl_s > 0 else None
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO approval_grants"
                "(session_id,tool,action,key_hash,decision,scope,uses_left,created_at,expires_at,note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id or "", tool, action or "", key_hash or "", decision,
                 scope, int(uses_left), now, expires, note[:500]),
            )
            return {"id": cur.lastrowid, "scope": scope, "decision": decision,
                    "expires_at": expires}

    def _match(self, row: sqlite3.Row, session_id: str, tool: str,
               action: str, key_hash: str) -> bool:
        if row["tool"] != tool:
            return False
        # accion vacia = wildcard de tool; si no, exacta.
        if row["action"] and row["action"] != action:
            return False
        # key vacia = politica por tool/accion (MCP always). Si no, exacta.
        if row["key_hash"] and row["key_hash"] != key_hash:
            return False
        if row["scope"] == "always":
            return True
        return bool(session_id) and row["session_id"] == session_id

    def check(self, *, session_id: str, tool: str, action: str,
              key_hash: str) -> Optional[str]:
        """Devuelve 'deny' | 'allow' | None. Deny siempre gana."""
        now = time.time()
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = self._conn.execute(
                "SELECT * FROM approval_grants WHERE tool=? ORDER BY id DESC",
                (tool,),
            ).fetchall()
            self._conn.row_factory = None
            matched: list[sqlite3.Row] = []
            for row in rows:
                if row["expires_at"] is not None and float(row["expires_at"]) <= now:
                    continue
                if int(row["uses_left"] or 0) <= 0:
                    continue
                if self._match(row, session_id, tool, action, key_hash):
                    matched.append(row)
            for row in matched:
                if row["decision"] == "deny":
                    return "deny"
            for row in matched:
                if row["decision"] == "allow":
                    if row["scope"] == "once":
                        self._consume(int(row["id"]))
                    return "allow"
        return None

    def _consume(self, grant_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE approval_grants SET uses_left=uses_left-1 WHERE id=? AND uses_left>0",
                (grant_id,),
            )
            self._conn.execute(
                "DELETE FROM approval_grants WHERE id=? AND uses_left<=0", (grant_id,)
            )

    def list(self, session_id: str = "", include_expired: bool = False) -> list[dict]:
        now = time.time()
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            if session_id:
                rows = self._conn.execute(
                    "SELECT * FROM approval_grants WHERE session_id=? OR scope='always'"
                    " ORDER BY id DESC", (session_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM approval_grants ORDER BY id DESC").fetchall()
            self._conn.row_factory = None
        out = []
        for r in rows:
            if not include_expired and r["expires_at"] is not None and float(r["expires_at"]) <= now:
                continue
            d = dict(r)
            d["expires_at"] = float(d["expires_at"]) if d["expires_at"] is not None else None
            out.append(d)
        return out

    def clear(self, session_id: str = "") -> int:
        with self._lock:
            if session_id:
                cur = self._conn.execute(
                    "DELETE FROM approval_grants WHERE session_id=?", (session_id,))
            else:
                cur = self._conn.execute("DELETE FROM approval_grants")
            return cur.rowcount or 0


class Approvals:
    """Cola en memoria + grants durables.

    La cola no se persiste a proposito: un pedido pendiente que sobrevive a un
    reinicio no tiene quien lo responda y debe fallar cerrado. Los grants SI.
    """

    def __init__(self, db_path: str | Path = "./data/approvals.db") -> None:
        self._items: dict[str, dict] = {}
        self._apply: dict[str, object] = {}
        self._lock = threading.RLock()
        self.store = GrantStore(db_path)
        self._on_new = []

    # -- notificaciones -------------------------------------------------
    def on_new(self, callback) -> None:
        """Callback sincrono al encolar un pedido (la UI ya hace polling)."""
        self._on_new.append(callback)

    # -- cola -----------------------------------------------------------
    def propose(
        self,
        session_id: str,
        tool: str,
        args: dict,
        preview: str = "",
        apply=None,
        *,
        action: str = "",
        key_hash: str = "",
        kind: str = "tool",
        source: str = "chat",
        timeout_s: float = 0,
        metadata: dict | None = None,
    ) -> dict:
        aid = uuid.uuid4().hex[:10]
        now = time.time()
        item = {
            "id": aid,
            "session_id": session_id,
            "tool": tool,
            "action": action or tool,
            "key_hash": key_hash or "",
            "kind": kind,
            "source": source,
            "args": {k: v for k, v in (args or {}).items()
                     if k not in ("content", "new", "patch")},
            "preview": (preview or "")[:4000],
            "status": "pending",
            "decision": "pending",
            "created_at": now,
            "expires_at": now + timeout_s if timeout_s and timeout_s > 0 else None,
            "metadata": metadata or {},
        }
        with self._lock:
            self._items[aid] = item
            if apply is not None:
                self._apply[aid] = apply
        for cb in list(self._on_new):
            try:
                cb(dict(item))
            except Exception:
                pass
        return dict(item)

    def list(self, session_id: str = "") -> list[dict]:
        with self._lock:
            items = [dict(v) for v in self._items.values()
                     if (not session_id or v["session_id"] == session_id)]
        return sorted(items, key=lambda x: x["created_at"], reverse=True)

    def get(self, aid: str) -> dict | None:
        with self._lock:
            it = self._items.get(aid)
            return dict(it) if it else None

    def _finish(self, aid: str, status: str) -> dict | None:
        with self._lock:
            it = self._items.get(aid)
            if not it:
                return None
            it["status"] = status
            it["decision"] = status
            it["responded_at"] = time.time()
            return dict(it)

    def resolve(
        self,
        aid: str,
        approve: bool,
        *,
        scope: str = "once",
        responder: str = "human",
        comment: str = "",
    ) -> dict:
        """Resuelve un pedido. `scope` decide el grant: once|session|always.

        Para denegar, `scope` puede ser `once` (solo este pedido), `session`
        (deny para la sesion) o `always` (deny global). El deny se guarda y
        gana sobre cualquier allow.
        """
        with self._lock:
            it = self._items.get(aid)
            if not it:
                return {"ok": False, "error": "no existe"}
            if it["status"] != "pending":
                return {"ok": False, "error": f"ya {it['status']}"}
            fn = self._apply.get(aid)
        status = "approved" if approve else "rejected"
        grant = None
        # MCP: session/always se recuerdan por server+tool (politica), no por
        # argumentos. `once` conserva el hash exacto de la llamada.
        _key = it.get("key_hash") or ""
        if _key and scope in ("session", "always") and str(it.get("action") or "").startswith("mcp:"):
            _key = ""
        try:
            if scope not in ("once", "session", "always"):
                scope = "once"
            if approve and not (scope == "once" and callable(fn)):
                # `once` con closure se ejecuta AHORA: no hace falta dejar un
                # grant de un uso suelto. Los pedidos del gate (sin closure) si
                # necesitan el grant para que `authorize` lo consuma.
                grant = self.store.add(
                    session_id=it["session_id"], tool=it["tool"],
                    action=it["action"], key_hash=_key,
                    decision="allow", scope=scope,
                    note=comment or f"aprobado por {responder}",
                )
            elif scope in ("session", "always"):
                grant = self.store.add(
                    session_id=it["session_id"], tool=it["tool"],
                    action=it["action"], key_hash=_key,
                    decision="deny", scope=scope,
                    note=comment or f"denegado por {responder}",
                )
        except Exception as e:
            if approve:
                return {"ok": False, "error": f"no se pudo guardar el grant: {e}"}
        self._finish(aid, status)
        if approve and callable(fn):
            try:
                out = fn()
                return {"ok": True, "status": "approved", "grant": grant,
                        "result": str(out)[:500]}
            except Exception as e:
                return {"ok": False, "error": str(e), "grant": grant}
        return {"ok": True, "status": status, "grant": grant}

    def expire(self, aid: str) -> bool:
        with self._lock:
            it = self._items.get(aid)
            if not it or it["status"] != "pending":
                return False
        self._finish(aid, "timed_out")
        return True

    async def wait(self, aid: str, timeout: float = 0) -> dict | None:
        """Espera (polling) a que el pedido se resuelva o expire."""
        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
        while True:
            it = self.get(aid)
            if it is None:
                return None
            if it["status"] != "pending":
                return it
            exp = it.get("expires_at")
            if exp and time.time() >= float(exp):
                self.expire(aid)
                return self.get(aid)
            if deadline is not None and time.monotonic() >= deadline:
                self.expire(aid)
                return self.get(aid)
            await asyncio.sleep(0.2)

    # -- grants ---------------------------------------------------------
    def check_grant(self, *, session_id: str, tool: str, action: str,
                    key_hash: str) -> Optional[str]:
        try:
            return self.store.check(session_id=session_id, tool=tool,
                                    action=action, key_hash=key_hash)
        except Exception:
            return None

    def list_grants(self, session_id: str = "") -> list[dict]:
        try:
            return self.store.list(session_id)
        except Exception:
            return []

    def clear_grants(self, session_id: str = "") -> int:
        try:
            return self.store.clear(session_id)
        except Exception:
            return 0

    def clear_resolved(self) -> int:
        with self._lock:
            n = 0
            for aid in [k for k, v in self._items.items() if v["status"] != "pending"]:
                self._items.pop(aid, None)
                self._apply.pop(aid, None)
                n += 1
            return n


_APPROVALS = Approvals()


def get_approvals() -> Approvals:
    return _APPROVALS
