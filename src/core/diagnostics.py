"""Diagnostics-in-the-loop (sustituto liviano de LSP).

Tras editar/escribir un archivo, corre un chequeo rapido por tipo (sintaxis/lint)
y devuelve un bloque `<file_diagnostics>` para inyectar en el resultado de la tool
(el modelo se autocorrige sin un round-trip extra). Inspirado en opencode.

Best-effort y sin dependencias nuevas: usa el interprete actual / stdlib.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_MAX = 2000


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def diagnostics_for(path: str | Path) -> str:
    """Devuelve un bloque de diagnostico (o "" si no aplica / todo ok)."""
    p = Path(path)
    if not p.is_file():
        return ""
    ext = p.suffix.lower()
    out = ""
    try:
        if ext == ".py":
            rc, msg = _run([sys.executable, "-m", "py_compile", str(p)])
            if rc != 0:
                out = msg
        elif ext == ".json":
            try:
                json.loads(p.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:
                out = f"JSON invalido: {e}"
        elif ext in (".js", ".mjs", ".cjs"):
            node = "node"
            try:
                subprocess.run([node, "--version"], capture_output=True, timeout=5)
                rc, msg = _run([node, "--check", str(p)])
                if rc != 0:
                    out = msg
            except Exception:
                pass  # node no disponible -> sin diagnostico
        elif ext in (".yaml", ".yml"):
            try:
                import yaml as _y
                _y.safe_load(p.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:
                out = f"YAML invalido: {e}"
    except Exception:
        return ""
    if not out:
        return ""
    if len(out) > _MAX:
        out = out[:_MAX] + "\n…[truncado]"
    return f"\n\n<file_diagnostics path=\"{p.name}\">\n{out}\n</file_diagnostics>"


def config_enabled(cfg: dict | None) -> bool:
    if cfg is None:
        try:
            from src.core.config import load_config
            cfg = load_config()
        except Exception:
            return True
    try:
        return bool((cfg or {}).get("agent_loop", {}).get("diagnostics", True))
    except Exception:
        return True
