"""MCP setup portable — instala/verifica los MCPs del manifest.

Punto de integracion para el CLI de setup de otro agente: SOLO por
import/llamada (este modulo no toca ningun CLI)::

    from src.core.mcp_setup import run_mcp_setup
    report = run_mcp_setup(fix=True)

Modos:
  fix=False -> solo verifica (binarios via PATH + env vars presentes).
               Nunca ejecuta instaladores ni toca la red.
  fix=True  -> ademas intenta instalar lo instalable (npx/uvx/docker)
               y hace handshake corto (--version/--help con timeout).

Estados por entrada: ok | missing | needs_key | manual | disabled | error.
El reporte agregado trae conteos en ``summary`` y ``ready=True`` cuando no
hay nada en missing/error (needs_key y manual no bloquean: son accion del
usuario, no fallo del setup).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

MANIFEST_REL = Path("data") / "mcp_bundle" / "manifest.json"
HANDSHAKE_TIMEOUT_S = 25

Runner = Callable[[list[str], int], tuple[int, str]]


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def manifest_path() -> Path:
    custom = os.environ.get("NEXUS_MCP_MANIFEST")
    if custom:
        return Path(custom).expanduser()
    return project_root() / MANIFEST_REL


def load_manifest(path: Optional[Path] = None) -> dict[str, Any]:
    p = path or manifest_path()
    return json.loads(p.read_text(encoding="utf-8"))


def _default_runner(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode, out[:2000]
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:  # pragma: no cover - defensivo
        return 1, str(e)[:500]


def _binary_for(entry: dict[str, Any]) -> Optional[str]:
    kind = entry.get("kind", "")
    cmd = entry.get("command") or []
    if kind in ("npx", "uvx", "docker") and cmd:
        return cmd[0]
    return None


def _missing_keys(entry: dict[str, Any], env: Optional[dict[str, str]] = None) -> list[str]:
    env = env if env is not None else os.environ
    required = entry.get("env_required") or []
    # NEXUS_SOVEREIGN_URL trae default loopback: solo falta si esta vacia.
    out = []
    for k in required:
        v = env.get(k, "")
        if k == "NEXUS_SOVEREIGN_URL":
            continue
        if not v:
            out.append(k)
    return out


def probe_entry(
    entry: dict[str, Any],
    *,
    fix: bool = False,
    runner: Optional[Runner] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Evalua una entrada del manifest. Sin red salvo fix=True."""
    name = entry.get("name", "?")
    kind = entry.get("kind", "")
    result: dict[str, Any] = {"name": name, "kind": kind, "status": "error", "detail": ""}
    try:
        if not entry.get("enabled_default", True):
            result.update(status="disabled", detail="deshabilitado por defecto")
            return result
        if kind == "manual":
            result.update(status="manual", detail=str(entry.get("install", ""))[:300])
            return result
        missing = _missing_keys(entry, env)
        if missing:
            result.update(status="needs_key", detail="faltan env: " + ",".join(missing),
                          missing_keys=missing)
            return result
        if kind == "remote-url":
            result.update(status="ok", detail="remoto configurado (sin handshake local)")
            return result
        binary = _binary_for(entry)
        resolve = which or shutil.which
        if binary and not resolve(binary):
            if fix:
                inst = _installable_command(entry)
                if inst is None:
                    result.update(status="manual", detail="sin instalador automatico")
                    return result
                run = runner or _default_runner
                rc, out = run(inst, HANDSHAKE_TIMEOUT_S)
                if rc == 0:
                    result.update(status="ok", detail="instalado ahora: " + out[:200])
                else:
                    result.update(status="missing", detail=f"instalador rc={rc}: " + out[:200])
                return result
            result.update(status="missing", detail=f"binario '{binary}' no en PATH")
            return result
        if fix:
            run = runner or _default_runner
            rc, out = run(_handshake_command(entry), HANDSHAKE_TIMEOUT_S)
            if rc == 0:
                result.update(status="ok", detail="handshake ok: " + out[:200])
            else:
                result.update(status="error", detail=f"handshake rc={rc}: " + out[:200])
            return result
        result.update(status="ok", detail="binario presente + env completa")
        return result
    except Exception as e:
        result.update(status="error", detail=f"{type(e).__name__}: {e}")
        return result


def _installable_command(entry: dict[str, Any]) -> Optional[list[str]]:
    """Comando que deja el paquete listo en cache local (sin arrancar el MCP)."""
    kind = entry.get("kind", "")
    cmd = list(entry.get("command") or [])
    if kind == "npx" and len(cmd) >= 3 and cmd[0] == "npx":
        pkg = cmd[2]
        return ["npx", "-y", pkg, "--help"]
    if kind == "uvx" and cmd and cmd[0] == "uvx":
        return cmd + ["--help"]
    if kind == "docker":
        return ["docker", "--version"]
    return None


def _handshake_command(entry: dict[str, Any]) -> list[str]:
    cmd = list(entry.get("command") or [])
    if not cmd:
        return ["echo", "remote"]
    if cmd[0] in ("npx", "uvx"):
        return cmd + ["--help"]
    if cmd[0] == "docker":
        return ["docker", "--version"]
    return [cmd[0], "--version"]


def run_mcp_setup(
    fix: bool = False,
    *,
    runner: Optional[Runner] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    env: Optional[dict[str, str]] = None,
    manifest: Optional[dict[str, Any]] = None,
    manifest_path_override: Optional[Path] = None,
) -> dict[str, Any]:
    """Instala/verifica cada MCP del manifest y devuelve el reporte agregado."""
    try:
        data = manifest if manifest is not None else load_manifest(manifest_path_override)
    except Exception as e:
        return {"ready": False, "fix": fix, "entries": [],
                "summary": {"error": 1}, "load_error": f"{type(e).__name__}: {e}"}
    entries = data.get("entries", [])
    results = [probe_entry(e, fix=fix, runner=runner, which=which, env=env) for e in entries]
    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    ready = summary.get("missing", 0) == 0 and summary.get("error", 0) == 0
    return {
        "ready": ready,
        "fix": fix,
        "manifest_version": data.get("version", 0),
        "entries": results,
        "summary": summary,
        "manual": [r["name"] for r in results if r["status"] == "manual"],
        "needs_key": [r["name"] for r in results if r["status"] == "needs_key"],
    }
