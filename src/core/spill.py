"""Spill de resultados de tools: preview head+tail inline y archivo completo.

Problema que resuelve: el loop recortaba a 8000 caracteres *el final* del
resultado, que es justo donde aparecen los errores de un build/test. El modelo
quedaba sin el dato y sin forma de recuperarlo.

Cada resultado grande se guarda completo en `data/spill/<sesión>/<tool>-<stamp>.txt`
y el mensaje inline conserva cabeza + cola + la ruta para que el agente pueda
volver a leerlo con `read_file`/`grep`.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

_DEFAULT_MAX_INLINE = 8000
_HEAD_RATIO = 0.62


def _spill_root() -> Path:
    try:
        from .environment import data_home
        return data_home() / "spill"
    except Exception:
        return Path(__file__).parents[2] / "data" / "spill"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:40] or "tool"


def spill_dir(session_id: str) -> Path:
    """Carpeta de spill de una sesión (el nombre es un hash, no el id).

    Público porque quien borra una sesión necesita resolver la misma carpeta que
    este módulo creó; recalcular el hash afuera es cómo se pierden archivos.
    """
    return _spill_root() / session_id_key(session_id)


def session_id_key(session_id: str) -> str:
    return hashlib.sha1((session_id or "default").encode("utf-8")).hexdigest()[:12]


def save_text(session_id: str, tool: str, text: str, call_id: str = "") -> Path | None:
    """Persiste el texto completo. Best-effort: si falla, no rompe el turno."""
    try:
        folder = spill_dir(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        suffix = f"_{_safe(call_id)[:12]}" if call_id else ""
        path = folder / f"{_safe(tool)}-{stamp}{suffix}.txt"
        path.write_text(text, encoding="utf-8", errors="replace")
        return path
    except Exception:
        return None


def clip_output(text: str, session_id: str = "", tool: str = "tool",
                call_id: str = "", max_inline: int = _DEFAULT_MAX_INLINE) -> str:
    """Devuelve el texto tal cual si entra; si no, cabeza+cola y locator del spill."""
    if len(text) <= max_inline:
        return text
    head_len = int(max_inline * _HEAD_RATIO)
    tail_len = max_inline - head_len
    head = text[:head_len]
    tail = text[-tail_len:]
    removed = len(text) - len(head) - len(tail)
    path = save_text(session_id, tool, text, call_id)
    if path is not None:
        try:
            from .environment import workspace_root
            locator = path.relative_to(workspace_root()).as_posix()
        except Exception:
            locator = path.as_posix()
        hint = (f"\n[... {removed} caracteres recortados del medio; salida completa en "
                f"{locator} — usá read_file con offset/limit o grep sobre esa ruta ...]\n")
    else:
        hint = f"\n[... {removed} caracteres recortados del medio (no se pudo persistir) ...]\n"
    return head + hint + tail
