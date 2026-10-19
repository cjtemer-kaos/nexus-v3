"""Plugins de terceros: módulos .py en ./plugins que exponen `register(registry)`.

Cada plugin puede registrar tools (u hooks) usando la API del ToolRegistry. Se cargan al
construir el registry (y on-demand vía /api/plugins/reload). Un error en un plugin NO rompe
el harness (se ignora ese plugin).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _base(project_root, dir_name: str = "plugins") -> Path:
    return Path(project_root) / dir_name


def list_plugins(project_root, dir_name: str = "plugins") -> list[dict]:
    base = _base(project_root, dir_name)
    out: list[dict] = []
    if base.is_dir():
        for f in sorted(base.glob("*.py")):
            if f.name.startswith("_"):
                continue
            out.append({"name": f.stem, "path": str(f)})
    return out


_last_report: dict = {"loaded": [], "errors": [], "dir": ""}


def last_report() -> dict:
    """Ultimo resultado de load_plugins: que plugins cargaron y cuales fallaron."""
    return {"loaded": list(_last_report.get("loaded") or []),
            "errors": list(_last_report.get("errors") or []),
            "dir": _last_report.get("dir") or ""}


def load_plugins(registry, project_root, dir_name: str = "plugins") -> list[str]:
    """Carga cada plugin y llama a su `register(registry)`. Devuelve los nombres cargados.

    Los errores NO rompen el arranque, pero quedan en `last_report()` para que
    /api/plugins/reload pueda informarlos en vez de devolver ok:true siempre.
    """
    global _last_report
    base = _base(project_root, dir_name)
    loaded: list[str] = []
    errors: list[dict] = []
    if not base.is_dir():
        _last_report = {"loaded": [], "errors": [], "dir": str(base)}
        return loaded
    for f in sorted(base.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"nexus_plugin_{f.stem}", str(f))
            if spec is None or spec.loader is None:
                errors.append({"plugin": f.stem, "error": "no se pudo crear el spec de import"})
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            reg = getattr(mod, "register", None)
            if callable(reg):
                reg(registry)
                loaded.append(f.stem)
            else:
                errors.append({"plugin": f.stem, "error": "no expone register(registry)"})
        except Exception as e:
            errors.append({"plugin": f.stem, "error": f"{type(e).__name__}: {e}"})
            continue
    _last_report = {"loaded": loaded, "errors": errors, "dir": str(base)}
    return loaded
