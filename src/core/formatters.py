"""Formateadores — dejar el archivo prolijo después de escribirlo.

Editar código sin formatear deja diffs con ruido: líneas que se movieron por
una coma, imports desordenados, comillas cambiadas. Eso ensucia el checkpoint y
hace que la próxima edición del modelo falle al buscar el texto exacto.

Este módulo resuelve, para una ruta, qué herramienta corresponde y la corre por
**argv** (nunca shell), con timeout y sin lanzar nunca: formatear es un extra,
no puede tumbar un turno.

Resolución por extensión:
- explícito en ``formatters.commands`` (manda siempre),
- ``.py`` → ``ruff`` del venv del proyecto, si no ``ruff``/``black`` del PATH,
- ``.js/.ts/.jsx/.tsx/.json/.css/.md`` → ``prettier`` local de ``node_modules``
  (no se usa ``npx``: descargaría paquetes en medio de un turno),
- ``.go`` → ``gofmt`` · ``.rs`` → ``rustfmt``.

Si la herramienta no está instalada, el archivo queda como estaba y se informa
el motivo. Nunca se inventa un formateador.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

MAX_DETAIL = 600
DEFAULT_TIMEOUT_S = 20.0

_PRETTIER_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".json", ".css", ".scss", ".md", ".html"}


@dataclass
class Formatter:
    name: str
    argv: List[str]

    def command_for(self, path: Path) -> List[str]:
        if any("{file}" in part for part in self.argv):
            return [part.replace("{file}", str(path)) for part in self.argv]
        return [*self.argv, str(path)]


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cfg_block(cfg: Optional[dict]) -> dict:
    if cfg is None:
        try:
            from src.core.config import load_raw_config

            cfg = load_raw_config() or {}
        except Exception:
            cfg = {}
    block = (cfg or {}).get("formatters")
    return block if isinstance(block, dict) else {}


def enabled(cfg: Optional[dict] = None) -> bool:
    block = _cfg_block(cfg)
    return bool(block.get("enabled", True))


def timeout_s(cfg: Optional[dict] = None) -> float:
    try:
        return float(_cfg_block(cfg).get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def _venv_bin(name: str) -> Optional[str]:
    """Ejecutable dentro del venv del proyecto (Windows: Scripts, resto: bin)."""
    for folder in ("Scripts", "bin"):
        for suffix in ("", ".exe"):
            candidate = project_root() / ".venv-ft314" / folder / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def _local_node_bin(name: str) -> Optional[str]:
    base = project_root() / "node_modules" / ".bin"
    for suffix in (".cmd", ".exe", ""):
        candidate = base / f"{name}{suffix}"
        if candidate.is_file():
            return str(candidate)
    return None


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _candidates(ext: str) -> Sequence[Formatter]:
    if ext == ".py":
        out: List[Formatter] = []
        for name in ("ruff", "black"):
            path = _venv_bin(name) or _which(name)
            if path:
                argv = [path, "format"] if name == "ruff" else [path]
                out.append(Formatter(name=name, argv=argv))
        return out
    if ext in _PRETTIER_EXT:
        path = _local_node_bin("prettier") or _which("prettier")
        return [Formatter(name="prettier", argv=[path, "--write"])] if path else []
    if ext == ".go":
        path = _which("gofmt")
        return [Formatter(name="gofmt", argv=[path, "-w"])] if path else []
    if ext == ".rs":
        path = _which("rustfmt")
        return [Formatter(name="rustfmt", argv=[path])] if path else []
    return []


def _raw_explicit(ext: str, cfg: Optional[dict]):
    raw = _cfg_block(cfg).get("commands")
    if not isinstance(raw, dict):
        return None
    return raw.get(ext) or raw.get(ext.lstrip("."))


def _explicit(ext: str, cfg: Optional[dict]) -> Optional[Formatter]:
    spec = _raw_explicit(ext, cfg)
    if not spec:
        return None
    if isinstance(spec, str):
        parts = [spec]
    elif isinstance(spec, (list, tuple)):
        parts = [str(p) for p in spec]
    else:
        return None
    parts = [p for p in parts if p]
    if not parts:
        return None
    exe = parts[0]
    if not (os.path.isabs(exe) or _which(exe) or _venv_bin(exe) or _local_node_bin(exe)):
        return None
    return Formatter(name=Path(exe).stem, argv=parts)


def resolve(path: str | Path, cfg: Optional[dict] = None) -> Optional[Formatter]:
    """Formateador que corresponde a ``path``, o ``None`` si no hay.

    Si la config declara uno para esa extensión, se respeta esa decisión: no se
    cae a la autodetección, porque un comando mal escrito debe verse, no taparse
    con otra herramienta.
    """
    ext = Path(path).suffix.lower()
    if not ext:
        return None
    if _raw_explicit(ext, cfg) is not None:
        return _explicit(ext, cfg)
    for candidate in _candidates(ext):
        return candidate
    return None


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _tail(text: str) -> str:
    clean = (text or "").strip()
    if len(clean) <= MAX_DETAIL:
        return clean
    return "…" + clean[-MAX_DETAIL:]


def format_file(path: str | Path, cfg: Optional[dict] = None, root: Optional[Path] = None) -> Dict[str, object]:
    """Formatea un archivo. Devuelve el resultado; nunca lanza.

    ``status`` es uno de: ``formatted`` (cambió), ``clean`` (ya estaba),
    ``skipped`` (sin formateador o deshabilitado) o ``error``.
    """
    started = time.monotonic()
    root = Path(root) if root is not None else project_root()
    target = Path(path)

    def done(status: str, formatter: str = "", detail: str = "") -> Dict[str, object]:
        return {
            "status": status,
            "file": str(target),
            "formatter": formatter,
            "detail": detail,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    if not enabled(cfg):
        return done("skipped", detail="formateo deshabilitado (formatters.enabled: false)")
    try:
        target = target.resolve()
    except (OSError, ValueError) as exc:
        return done("error", detail=f"ruta inválida: {exc}")
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return done("skipped", detail="fuera del proyecto: no se formatea")
    if not target.is_file():
        return done("skipped", detail="no existe")

    fmt = resolve(target, cfg)
    if fmt is None:
        if _raw_explicit(target.suffix.lower(), cfg) is not None:
            return done(
                "error",
                detail=f"el formateador configurado para {target.suffix} no existe o no es ejecutable",
            )
        return done("skipped", detail=f"sin formateador para {target.suffix or '(sin extensión)'}")

    before = _digest(target)
    argv = fmt.command_for(target)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s(cfg),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return done("error", fmt.name, f"timeout tras {timeout_s(cfg):.0f}s")
    except FileNotFoundError:
        return done("error", fmt.name, "no se encontró el ejecutable")
    except OSError as exc:
        return done("error", fmt.name, f"{type(exc).__name__}: {exc}")

    if proc.returncode != 0:
        detail = _tail(proc.stderr) or _tail(proc.stdout) or f"exit {proc.returncode}"
        return done("error", fmt.name, detail)
    after = _digest(target)
    if before and after and before != after:
        return done("formatted", fmt.name)
    return done("clean", fmt.name)


def format_files(paths: Sequence[str | Path], cfg: Optional[dict] = None, root: Optional[Path] = None) -> List[Dict[str, object]]:
    """Formatea varios archivos; los errores no cortan el resto."""
    return [format_file(p, cfg=cfg, root=root) for p in paths]


def summarize(results: Sequence[Dict[str, object]]) -> str:
    """Una línea para el log o el resultado de la tool."""
    changed = sum(1 for r in results if r.get("status") == "formatted")
    errors = [r for r in results if r.get("status") == "error"]
    parts = [f"{changed} formateado(s)"]
    if errors:
        first = errors[0]
        parts.append(f"{len(errors)} error(es): {first.get('file')}: {first.get('detail')}")
    return " · ".join(parts)
