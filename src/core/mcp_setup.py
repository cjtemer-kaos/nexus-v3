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

Estados por entrada: ok | missing | needs_key | manual | disabled | remote | error.
``remote`` = servidor remoto (HTTP/SSE) valido y configurado, pero el cliente
MCP interno de nexus-v3 solo lanza servidores stdio: no se puede usar solo con
nexus-v3 (usalo desde un cliente que ya tenga ese servidor).
El reporte agregado trae conteos en ``summary`` y ``ready=True`` cuando no
hay nada en missing/error (needs_key, manual y remote no bloquean: son accion
del usuario o limitacion conocida, no fallo del setup).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

MANIFEST_REL = Path("data") / "mcp_bundle" / "manifest.json"
STATUS_REL = Path("data") / "mcp_bundle" / "status.json"
HANDSHAKE_TIMEOUT_S = 25
# npx/uvx pueden tener que DESCARGAR el paquete en el primer uso: con 25s da
# timeout y el server queda marcado como error aunque funcione (paso con
# computer-control en una instalacion limpia).
DOWNLOAD_TIMEOUT_S = 300

Runner = Callable[[list[str], int], tuple[int, str]]


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def status_path() -> Path:
    return project_root() / STATUS_REL


def resolve_tool(name: str, *, root: Optional[Path] = None,
                 env: Optional[dict[str, str]] = None) -> Optional[str]:
    """Resuelve `npx`/`uvx`/`docker` a una ruta concreta.

    Precedencia: NEXUS_<TOOL> (env) > venv del proyecto > PATH.
    El venv se consulta primero porque el bootstrap instala ahi un `uv`
    propio: en maquinas donde el uvx del sistema esta roto (p.ej. os error
    448 en Windows) el MCP igual funciona.
    """
    env = env if env is not None else os.environ
    root = Path(root) if root is not None else project_root()
    override = env.get("NEXUS_" + name.upper())
    if override and Path(override).exists():
        return override
    if os.name == "nt":
        exe = name + ".exe"
        for candidate in (root / ".venv-ft314" / "Scripts" / exe,
                          root / ".venv" / "Scripts" / exe,
                          root / ".nexus-mcp" / "Scripts" / exe):
            if candidate.exists():
                return str(candidate)
    else:
        for candidate in (root / ".venv-ft314" / "bin" / name,
                          root / ".venv" / "bin" / name,
                          root / ".nexus-mcp" / "bin" / name):
            if candidate.exists():
                return str(candidate)
    return shutil.which(name, path=env.get("PATH"))


def resolve_command(entry: dict[str, Any], *,
                    root: Optional[Path] = None,
                    env: Optional[dict[str, str]] = None) -> list[str]:
    """Comando ejecutable de una entrada del manifest, con el binario resuelto.

    Asi el runtime (`mcp_client`) no depende del PATH del shell que arranco
    la app: usa el mismo `uvx`/`npx` que valido el setup.
    """
    cmd = list(entry.get("command") or [])
    if not cmd:
        return []
    resolved = resolve_tool(cmd[0], root=root, env=env)
    if resolved:
        cmd[0] = resolved
    return cmd


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
    resolve: bool = False,
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
            # El cliente MCP interno solo lanza servidores stdio (no hay transporte
            # HTTP): el servidor es valido, pero nexus-v3 por si solo no lo puede
            # llamar. Se reporta como "remote" para no prometer algo que no hace.
            result.update(status="remote",
                          detail="remoto configurado; nexus-v3 solo lanza MCP stdio "
                                 "(sin transporte HTTP) — usalo desde un cliente que "
                                 "ya tenga ese servidor")
            return result
        binary = _binary_for(entry)
        resolve_fn = which or shutil.which
        if resolve and binary:
            found = resolve_tool(binary)
            if found:
                result["binary_path"] = found
                binary = found
        # npx/uvx pueden descargar el paquete en el primer uso: timeout largo.
        timeout_s = (DOWNLOAD_TIMEOUT_S if entry.get("kind") in ("npx", "uvx")
                     else HANDSHAKE_TIMEOUT_S)
        if binary and not resolve_fn(binary):
            if fix:
                inst = _installable_command(entry, resolve=resolve)
                if inst is None:
                    result.update(status="manual", detail="sin instalador automatico")
                    return result
                run = runner or _default_runner
                rc, out = run(inst, timeout_s)
                if rc == 0:
                    result.update(status="ok", detail="instalado ahora: " + out[:200])
                else:
                    result.update(status="missing", detail=f"instalador rc={rc}: " + out[:200])
                return result
            result.update(status="missing", detail=f"binario '{binary}' no en PATH")
            return result
        if fix:
            run = runner or _default_runner
            rc, out = run(_handshake_command(entry, resolve=resolve), timeout_s)
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


def _installable_command(entry: dict[str, Any], resolve: bool = False) -> Optional[list[str]]:
    """Comando que deja el paquete listo en cache local (sin arrancar el MCP)."""
    kind = entry.get("kind", "")
    cmd = list(entry.get("command") or [])
    if resolve and cmd:
        cmd = resolve_command(entry)
    if kind == "npx" and len(cmd) >= 3 and cmd[0].endswith(("npx", "npx.exe", "npx.cmd")):
        pkg = cmd[2]
        return [cmd[0], "-y", pkg, "--help"]
    if kind == "npx" and len(cmd) >= 3 and cmd[0] == "npx":
        pkg = cmd[2]
        return ["npx", "-y", pkg, "--help"]
    if kind == "uvx" and cmd and cmd[0].endswith(("uvx", "uvx.exe")):
        return cmd + ["--help"]
    if kind == "uvx" and cmd and cmd[0] == "uvx":
        return cmd + ["--help"]
    if kind == "docker":
        return ["docker", "--version"]
    return None


def _handshake_command(entry: dict[str, Any], resolve: bool = False) -> list[str]:
    cmd = resolve_command(entry) if resolve else list(entry.get("command") or [])
    if not cmd:
        return ["echo", "remote"]
    base = os.path.basename(cmd[0]).lower()
    if base.startswith(("npx", "uvx")):
        return cmd + ["--help"]
    if base.startswith("docker"):
        return ["docker", "--version"]
    return [cmd[0], "--version"]


UV_HINT = Path(".venv-ft314") / "Scripts" / "uv.exe"


def ensure_uv(root: Optional[Path] = None,
              runner: Optional[Runner] = None,
              env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Deja un `uv`/`uvx` propio del proyecto en el venv.

    En Windows el uvx del sistema puede fallar (os error 448); instalar uv
    con pip dentro del venv da un binario confiable sin tocar el sistema.
    """
    root = root or project_root()
    env = env if env is not None else os.environ
    uvx = resolve_tool("uvx", root=root, env=env)
    if uvx:
        return {"ok": True, "path": uvx, "action": "ya disponible"}
    py = root / ".venv-ft314" / "Scripts" / "python.exe"
    if not py.exists():
        py = root / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        return {"ok": False, "path": "", "action": "sin venv del proyecto"}
    run = runner or _default_runner
    rc, out = run([str(py), "-m", "pip", "install", "--quiet", "uv"], 180)
    uvx = resolve_tool("uvx", root=root, env=env)
    return {"ok": bool(uvx), "path": uvx or "",
            "action": f"pip install uv rc={rc}", "detail": out[:300]}


def write_status(report: dict[str, Any], path: Optional[Path] = None) -> Path:
    """Persiste el ultimo reporte para que la UI/doctor lo muestren sin reinstalar."""
    p = path or status_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(report)
        payload["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return p


def run_mcp_setup(
    fix: bool = False,
    *,
    runner: Optional[Runner] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    env: Optional[dict[str, str]] = None,
    manifest: Optional[dict[str, Any]] = None,
    manifest_path_override: Optional[Path] = None,
    resolve: bool = False,
    ensure_uv_binary: bool = False,
) -> dict[str, Any]:
    """Instala/verifica cada MCP del manifest y devuelve el reporte agregado."""
    uv_info: dict[str, Any] = {}
    if ensure_uv_binary and fix:
        uv_info = ensure_uv(runner=runner)
    try:
        data = manifest if manifest is not None else load_manifest(manifest_path_override)
    except Exception as e:
        return {"ready": False, "fix": fix, "entries": [],
                "summary": {"error": 1}, "load_error": f"{type(e).__name__}: {e}"}
    entries = data.get("entries", [])
    results = [probe_entry(e, fix=fix, runner=runner, which=which, env=env, resolve=resolve)
               for e in entries]
    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    ready = summary.get("missing", 0) == 0 and summary.get("error", 0) == 0
    report = {
        "ready": ready,
        "fix": fix,
        "manifest_version": data.get("version", 0),
        "entries": results,
        "summary": summary,
        "manual": [r["name"] for r in results if r["status"] == "manual"],
        "needs_key": [r["name"] for r in results if r["status"] == "needs_key"],
    }
    if uv_info:
        report["uv"] = uv_info
    return report
