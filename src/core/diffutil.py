"""Diff unificado (para review/undo). Sin dependencias: difflib."""
from __future__ import annotations

import difflib
from pathlib import Path


def unified(a: str, b: str, name_a: str = "a", name_b: str = "b", context: int = 3) -> str:
    al = (a or "").splitlines()
    bl = (b or "").splitlines()
    return "\n".join(difflib.unified_diff(al, bl, fromfile=name_a, tofile=name_b, lineterm="", n=context))


def line_diff(old: str, new: str, max_lines: int = 80) -> str:
    """Diff compacto y numerado para devolverle al modelo tras un edit.

    Recorta prefijo/sufijo comun
    para emitir solo la region cambiada (un compare posicional ingenuo desalinea
    todo despues de un cambio de cantidad de lineas) y reparte el presupuesto
    entre borrados y agregados. Devuelve "" si no hay cambios.
    """
    old_lines = (old or "").splitlines()
    new_lines = (new or "").splitlines()
    start = 0
    while (start < len(old_lines) and start < len(new_lines)
           and old_lines[start] == new_lines[start]):
        start += 1
    old_end, new_end = len(old_lines), len(new_lines)
    while (old_end > start and new_end > start
           and old_lines[old_end - 1] == new_lines[new_end - 1]):
        old_end -= 1
        new_end -= 1
    removed, added = old_end - start, new_end - start
    if removed == 0 and added == 0:
        return ""
    removed_budget, added_budget = removed, added
    if removed + added > max_lines:
        # Reparto justo: ningun lado se come todo el presupuesto del otro.
        removed_budget = min(removed, max((max_lines + 1) // 2, max_lines - added))
        added_budget = min(added, max_lines - removed_budget)
    out = ["```diff"]
    out.extend(f"-{i + 1}: {old_lines[i]}" for i in range(start, start + removed_budget))
    out.extend(f"+{i + 1}: {new_lines[i]}" for i in range(start, start + added_budget))
    omitted = (removed - removed_budget) + (added - added_budget)
    if omitted:
        out.append(f"... diff truncado ({omitted} lineas omitidas) ...")
    out.append("```")
    return "\n".join(out)


def checkpoint_diff(session_id: str, checkpoint_id: str) -> dict:
    """Diff entre el contenido del checkpoint (original) y el archivo actual."""
    from src.core.checkpoints import list_checkpoints
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
