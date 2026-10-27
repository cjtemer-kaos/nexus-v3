"""Hooks por archivo.

Un hook es un ejecutable dentro de `.nexus/hooks/<fase>/` (o un archivo
`<fase>.py` en la raiz del directorio). Recibe el payload de la fase como JSON
por stdin y puede responder por stdout:

    {"action": "continue" | "stop" | "inject", "message": "...",
     "inject_content": "...", "metadata": {...}}

Convenciones:
- exit code 2 => STOP (bloquea la tool / corta la fase);
- exit code 0 sin JSON => CONTINUE;
- timeout o error de ejecucion => CONTINUE por defecto (`fail_closed=false`).

El directorio es opcional: si no existe, no se registra ningun handler y el
comportamiento del harness no cambia. Los scripts SIEMPRE se resuelven dentro
del workspace; una ruta que escape se ignora.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hooks_engine import HookAction, HookResult, HooksEngine
from .process_tree import run_process_capture

logger = logging.getLogger("nexus-hooks.files")

DEFAULT_DIR = ".nexus/hooks"
DEFAULT_TIMEOUT_S = 10.0
_MAX_MESSAGE = 2000
_MAX_INJECT = 8000


@dataclass(frozen=True)
class FileHook:
    phase: str
    path: Path


def _command_for(path: Path) -> list[str] | None:
    """Intérprete segun extension. None = no ejecutable / runtime ausente."""
    ext = path.suffix.lower()
    if ext == ".py":
        return [sys.executable, str(path)]
    if ext == ".ps1":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)] if exe else None
    if ext in (".cmd", ".bat"):
        return ["cmd", "/c", str(path)] if os.name == "nt" else None
    if ext == ".sh":
        exe = shutil.which("bash") or shutil.which("sh")
        return [exe, str(path)] if exe else None
    if ext == ".js":
        exe = shutil.which("node")
        return [exe, str(path)] if exe else None
    if ext == ".exe":
        return [str(path)]
    if os.name != "nt" and os.access(path, os.X_OK):
        return [str(path)]
    return None


def discover(directory: Path) -> dict[str, list[FileHook]]:
    """Descubre hooks por fase. Acepta `<dir>/<fase>/<archivo>` y `<dir>/<fase>.<ext>`."""
    directory = Path(directory)
    if not directory.is_dir():
        return {}
    found: dict[str, list[FileHook]] = {}
    valid = HooksEngine.VALID_PHASES
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir():
            if entry.name not in valid:
                continue
            candidates = sorted(
                (f for f in entry.iterdir()
                 if f.is_file() and not f.name.startswith((".", "_"))),
                key=lambda p: p.name.lower(),
            )
            for f in candidates:
                found.setdefault(entry.name, []).append(FileHook(entry.name, f))
        elif entry.is_file() and entry.stem in valid and not entry.name.startswith((".", "_")):
            found.setdefault(entry.stem, []).append(FileHook(entry.stem, entry))
    return found


def _safe_dir(raw: str, workspace_root: Path) -> Path | None:
    p = Path(raw or DEFAULT_DIR).expanduser()
    if not p.is_absolute():
        p = workspace_root / p
    try:
        p = p.resolve()
        p.relative_to(workspace_root.resolve())
    except Exception:
        logger.warning("hooks.files.dir fuera del workspace, se ignora: %s", raw)
        return None
    return p


def _parse_response(stdout: str) -> HookResult | None:
    """Busca la ultima linea JSON valida del stdout."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        action_raw = str(data.get("action") or "continue").strip().lower()
        if action_raw == "block":
            action_raw = "stop"
        try:
            action = HookAction(action_raw)
        except ValueError:
            logger.warning("hook devolvio action desconocida: %s", action_raw)
            return None
        return HookResult(
            action=action,
            message=str(data.get("message") or "")[:_MAX_MESSAGE],
            inject_content=str(data.get("inject_content") or "")[:_MAX_INJECT],
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        )
    return None


async def _run_one(
    hook: FileHook,
    payload: dict[str, Any],
    workspace_root: Path,
    timeout_s: float,
    fail_closed: bool,
) -> HookResult | None:
    command = _command_for(hook.path)
    if command is None:
        logger.debug("hook sin runtime ejecutable: %s", hook.path)
        return None
    body = dict(payload or {})
    body["phase"] = hook.phase
    body.setdefault("cwd", str(workspace_root))
    stdin_text = json.dumps(body, ensure_ascii=False, default=str)
    try:
        rc, out, err = await run_process_capture(
            command, workspace_root, timeout_s, stdin_text)
    except TimeoutError as e:
        logger.warning("hook timeout %s: %s", hook.path, e)
        if fail_closed:
            return HookResult(action=HookAction.STOP,
                              message=f"hook {hook.path.name} excedio {timeout_s}s")
        return None
    except Exception as e:
        logger.warning("hook no se pudo ejecutar %s: %s", hook.path, e)
        if fail_closed:
            return HookResult(action=HookAction.STOP,
                              message=f"hook {hook.path.name} no se pudo ejecutar")
        return None

    if rc == 2:
        detail = (err or out or "").strip()[-_MAX_MESSAGE:]
        return HookResult(
            action=HookAction.STOP,
            message=f"hook {hook.path.name} bloqueo la accion"
                    + (f": {detail}" if detail else ""),
        )
    if rc != 0:
        logger.warning("hook %s salio con %s: %s", hook.path, rc, (err or out)[:500])
        if fail_closed:
            return HookResult(action=HookAction.STOP,
                              message=f"hook {hook.path.name} fallo (exit {rc})")
        return None

    result = _parse_response(out)
    if result is not None:
        return result
    return None


def install_file_hooks(
    hooks: HooksEngine,
    workspace_root: str | Path,
    config: dict | None = None,
) -> int:
    """Descubre y registra hooks por archivo. Devuelve la cantidad registrada."""
    cfg = ((config or {}).get("files") if isinstance(config, dict) else None) or {}
    if not bool(cfg.get("enabled", True)):
        return 0
    if getattr(hooks, "_file_hooks_loaded", False):
        return 0
    root = Path(workspace_root).resolve()
    directory = _safe_dir(str(cfg.get("dir") or DEFAULT_DIR), root)
    if directory is None:
        return 0
    specs = discover(directory)
    if not specs:
        hooks._file_hooks_loaded = True  # type: ignore[attr-defined]
        return 0
    try:
        timeout_s = max(0.5, min(120.0, float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))))
    except Exception:
        timeout_s = DEFAULT_TIMEOUT_S
    fail_closed = bool(cfg.get("fail_closed", False))
    registered = 0

    for phase, phase_specs in specs.items():
        async def _handler(phase_name: str, payload: dict, _specs=tuple(phase_specs)):
            last = HookResult(action=HookAction.CONTINUE)
            for spec in _specs:
                res = await _run_one(spec, payload, root, timeout_s, fail_closed)
                if res is None:
                    continue
                if res.action is HookAction.INJECT:
                    last = res
                    continue
                if res.action in (HookAction.STOP, HookAction.COMPACT):
                    return res
            return last

        hooks.on(phase, _handler)
        registered += len(phase_specs)

    hooks._file_hooks_loaded = True  # type: ignore[attr-defined]
    if registered:
        logger.info("hooks por archivo: %s scripts en %s", registered, directory)
    return registered
