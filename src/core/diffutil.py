"""Diff unificado (para review/undo). Sin dependencias: difflib."""
from __future__ import annotations

import difflib
from pathlib import Path


def unified(a: str, b: str, name_a: str = "a", name_b: str = "b", context: int = 3) -> str:
    al = (a or "").splitlines()
    bl = (b or "").splitlines()
    return "\n".join(difflib.unified_diff(al, bl, fromfile=name_a, tofile=name_b, lineterm="", n=context))


def checkpoint_diff(session_id: str, checkpoint_id: str) -> dict:
    """Diff entre el contenido del checkpoint (original) y el archivo actual."""
    from src.core.checkpoints import list_checkpoints
    base = None
    orig = None
    for c in list_checkpoints(session_id, limit=10000):
        if c.get("id") == checkpoint_id:
            orig = c.get("path")
            break
    if not orig:
        return {"ok": False, "error": "checkpoint no encontrado"}
    # el snapshot vive en data/checkpoints/<session>/<id>
    import os
    from src.core.checkpoints import _session_dir
    snap = _session_dir(session_id) / checkpoint_id
    if not snap.is_file():
        return {"ok": False, "error": "snapshot no encontrado"}
    old = snap.read_text(encoding="utf-8", errors="replace")
    try:
        new = Path(orig).read_text(encoding="utf-8", errors="replace")
    except Exception:
        new = ""
    return {"ok": True, "path": orig, "diff": unified(old, new, name_a=checkpoint_id, name_b=orig)}
