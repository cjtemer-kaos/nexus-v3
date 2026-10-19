"""Checkpoints: snapshot del archivo ANTES de write/edit, con restore.

Da "undo" a las mutaciones del agente (inspirado en opencode/cline/Claude Code).
Snapshots en ./data/checkpoints/<session>/<ts>__<name>__<hash> + index.jsonl.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

_ROOT = Path(os.environ.get("NEXUS_CHECKPOINTS_DIR") or "./data/checkpoints")


def _session_dir(session_id: str) -> Path:
    return _ROOT / (session_id or "default")


def snapshot(session_id: str, path: str | Path) -> str | None:
    """Guarda el contenido actual de `path` (si existe). Devuelve el id o None."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        base = _session_dir(session_id)
        base.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        # sufijo sub-segundo: dos snapshots del mismo archivo en el mismo segundo
        # no deben pisarse (si no, se pierde el undo intermedio).
        uniq = f"{time.time_ns() % 1_000_000:06d}"
        h = hashlib.sha1(str(p.resolve()).encode("utf-8", "replace")).hexdigest()[:8]
        dest = base / f"{ts}_{uniq}__{p.name}__{h}"
        shutil.copy2(p, dest)
        with (base / "index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": dest.name, "path": str(p), "ts": ts}, ensure_ascii=False) + "\n")
        return dest.name
    except Exception:
        return None


def list_checkpoints(session_id: str, limit: int = 200) -> list[dict]:
    idx = _session_dir(session_id) / "index.jsonl"
    if not idx.exists():
        return []
    out: list[dict] = []
    for line in idx.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out[-limit:]


def restore(session_id: str, checkpoint_id: str) -> dict:
    base = _session_dir(session_id)
    src = base / checkpoint_id
    if not src.is_file():
        return {"ok": False, "error": "checkpoint no encontrado"}
    orig = None
    for c in list_checkpoints(session_id, limit=10000):
        if c.get("id") == checkpoint_id:
            orig = c.get("path")
            break
    if not orig:
        return {"ok": False, "error": "path original no encontrado"}
    try:
        shutil.copy2(src, orig)
        return {"ok": True, "restored": orig}
    except Exception as e:
        return {"ok": False, "error": str(e)}
