"""Cola de aprobaciones de mutaciones (modo auto_apply=approve). In-memory.

El agente PROPONE una mutación (write/edit) en vez de aplicarla; el usuario aprueba o
rechaza desde la UI (o vía endpoints). Al aprobar, se ejecuta la closure guardada.
"""
from __future__ import annotations

import threading
import time
import uuid


class Approvals:
    def __init__(self) -> None:
        self._items: dict[str, dict] = {}
        self._apply: dict[str, object] = {}
        self._lock = threading.Lock()

    def propose(self, session_id: str, tool: str, args: dict, preview: str = "", apply=None) -> dict:
        aid = uuid.uuid4().hex[:10]
        with self._lock:
            self._items[aid] = {
                "id": aid, "session_id": session_id, "tool": tool,
                "args": {k: v for k, v in (args or {}).items() if k not in ("content", "new")},
                "preview": (preview or "")[:4000], "status": "pending",
                "created_at": time.time(),
            }
            if apply is not None:
                self._apply[aid] = apply
        return dict(self._items[aid])

    def list(self, session_id: str = "") -> list[dict]:
        with self._lock:
            items = [dict(v) for v in self._items.values()
                     if (not session_id or v["session_id"] == session_id)]
        return sorted(items, key=lambda x: x["created_at"], reverse=True)

    def get(self, aid: str) -> dict | None:
        with self._lock:
            it = self._items.get(aid)
            return dict(it) if it else None

    def resolve(self, aid: str, approve: bool) -> dict:
        with self._lock:
            it = self._items.get(aid)
            if not it:
                return {"ok": False, "error": "no existe"}
            if it["status"] != "pending":
                return {"ok": False, "error": f"ya {it['status']}"}
            fn = self._apply.get(aid)
            it["status"] = "approved" if approve else "rejected"
        if approve and callable(fn):
            try:
                out = fn()
                return {"ok": True, "status": "approved", "result": str(out)[:500]}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": True, "status": it["status"]}

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
