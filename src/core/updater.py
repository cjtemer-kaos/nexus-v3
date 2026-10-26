"""Deteccion de updates de codigo por manifiesto (sin auto-apply).

El manifiesto lista archivos versionados con su sha256. Comparar es solo
lectura: nunca descarga ni reemplaza archivos. La aplicacion de un update
queda en manos del usuario (git pull / instalador).

Los hashes se calculan sobre el contenido con finales de linea normalizados
(CRLF -> LF). Sin eso, un clon de git en Windows con `core.autocrlf=true`
aparece como "desactualizado" con TODOS los archivos distintos aunque el
contenido sea identico al del commit publicado.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "nexus.update.manifest/v1"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_FILES = 20_000
MAX_REPORT = 300

EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", ".venv", ".venv-ft314", "venv", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "data",
    "brain", "workspace", "logs", "dist", "build", "_attic", ".hermes",
    ".playwright-mcp", "vendor",
}
EXCLUDE_SUFFIXES = {
    ".db", ".db-wal", ".db-shm", ".log", ".pyc", ".pyo", ".map", ".tmp",
    ".zip", ".7z", ".exe", ".dll", ".pyd", ".so", ".dylib",
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iter_normalized(path: Path, chunk: int = 1024 * 1024):
    """Lee el archivo normalizando CRLF -> LF sin cargarlo entero en memoria.

    Un `\\r` al final del bloque se retiene para no partir un CRLF entre chunks.
    """
    carry = b""
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            data = carry + block
            carry = b""
            if data.endswith(b"\r"):
                carry = b"\r"
                data = data[:-1]
            yield data.replace(b"\r\n", b"\n")
    if carry:
        yield carry


def _digest_file(path: Path) -> tuple[str, int]:
    """(sha256 del contenido normalizado, bytes normalizados)."""
    h = hashlib.sha256()
    size = 0
    for piece in _iter_normalized(path):
        h.update(piece)
        size += len(piece)
    return h.hexdigest(), size


def _sha256_file(path: Path) -> str:
    return _digest_file(path)[0]


def _canonical_hash(files: list[dict[str, Any]]) -> str:
    payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _project_version(root: Path) -> str:
    try:
        from importlib.metadata import version as _v
        return _v("nexus-v3")
    except Exception:
        pass
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        if pkg.get("version"):
            return str(pkg["version"])
    except Exception:
        pass
    try:
        import re
        txt = (root / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', txt, re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "unknown"


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                             text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _safe_rel(path: str) -> str:
    p = str(path or "").replace("\\", "/").strip()
    if not p or p.startswith("/") or ":" in p.split("/")[0]:
        raise ValueError(f"ruta invalida en manifiesto: {path!r}")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise ValueError(f"ruta fuera del proyecto en manifiesto: {path!r}")
    return "/".join(parts)


def _skip(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return True
    return Path(rel).suffix.lower() in EXCLUDE_SUFFIXES


def _git_files(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "-z")
    if not raw:
        return []
    return [p for p in raw.split("\0") if p]


def _walk_files(root: Path) -> list[str]:
    out: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        if not _skip(rel):
            out.append(rel)
    return out


def build_manifest(root: str | Path, *, version: str = "", commit: str = "",
                   files: Iterable[str] | None = None) -> dict[str, Any]:
    base = Path(root).resolve()
    rels = list(files) if files is not None else (_git_files(base) or _walk_files(base))
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rel in sorted(rels):
        try:
            safe = _safe_rel(rel)
        except ValueError:
            continue
        if safe in seen or _skip(safe):
            continue
        p = base / safe
        if not p.is_file():
            continue
        seen.add(safe)
        sha, size = _digest_file(p)
        entries.append({
            "path": safe,
            "sha256": sha,
            "size": size,
        })
        if len(entries) >= MAX_FILES:
            break
    if not version:
        version = _project_version(base)
    if not commit:
        commit = _git(base, "rev-parse", "--short", "HEAD")
    return {
        "schema": SCHEMA,
        "created_at": _now_iso(),
        "version": version or "unknown",
        "commit": commit or "",
        "counts": {"files": len(entries)},
        "files": entries,
        "content_hash": _canonical_hash(entries),
        "knowledge": {"bundle_schema": "nexus.knowledge.bundle/v1"},
    }


def validate_manifest(m: Any) -> None:
    if not isinstance(m, dict):
        raise ValueError("manifiesto debe ser un objeto JSON")
    if m.get("schema") != SCHEMA:
        raise ValueError(f"schema no soportado: {m.get('schema')!r} (esperado {SCHEMA})")
    files = m.get("files")
    if not isinstance(files, list):
        raise ValueError("files debe ser una lista")
    if len(files) > MAX_FILES:
        raise ValueError(f"manifiesto excede {MAX_FILES} archivos")
    seen: set[str] = set()
    for f in files:
        if not isinstance(f, dict):
            raise ValueError("cada archivo debe ser un objeto")
        rel = _safe_rel(str(f.get("path") or ""))
        if rel in seen:
            raise ValueError(f"ruta duplicada en manifiesto: {rel}")
        seen.add(rel)
        sha = str(f.get("sha256") or "").strip().lower()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError(f"sha256 invalido para {rel}")
    expected = m.get("content_hash")
    if expected and _canonical_hash(files) != expected:
        raise ValueError("content_hash no coincide: manifiesto corrupto o editado")


def load_manifest(source: str | Path, timeout: float = 10.0) -> dict[str, Any]:
    src = str(source).strip()
    if not src:
        raise ValueError("falta manifest_url")
    if src.lower().startswith(("http://", "https://")):
        import urllib.request
        req = urllib.request.Request(src, headers={"User-Agent": "nexus-v3-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("manifiesto demasiado grande")
        data = json.loads(raw.decode("utf-8"))
    else:
        p = Path(src)
        if p.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("manifiesto demasiado grande")
        data = json.loads(p.read_text(encoding="utf-8"))
    validate_manifest(data)
    return data


def compare(manifest: dict[str, Any], root: str | Path) -> dict[str, Any]:
    validate_manifest(manifest)
    base = Path(root).resolve()
    changed: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    unchanged = 0
    for f in manifest["files"]:
        rel = f["path"]
        local = base / rel
        try:
            local.resolve().relative_to(base)
        except ValueError:
            raise ValueError(f"ruta fuera del proyecto: {rel}")
        if not local.is_file():
            missing.append({"path": rel, "remote_sha256": f["sha256"]})
            continue
        local_sha, local_size = _digest_file(local)
        if local_sha != f["sha256"]:
            changed.append({"path": rel, "local_sha256": local_sha,
                            "remote_sha256": f["sha256"],
                            "local_size": local_size,
                            "remote_size": int(f.get("size") or 0)})
        else:
            unchanged += 1
    truncated = len(changed) > MAX_REPORT or len(missing) > MAX_REPORT
    return {
        "ok": True,
        "update_available": bool(changed or missing),
        "counts": {
            "total": len(manifest["files"]),
            "changed": len(changed),
            "missing": len(missing),
            "unchanged": unchanged,
        },
        "changed": changed[:MAX_REPORT],
        "missing": missing[:MAX_REPORT],
        "truncated": truncated,
        "remote": {
            "version": manifest.get("version"),
            "commit": manifest.get("commit"),
            "created_at": manifest.get("created_at"),
            "content_hash": manifest.get("content_hash"),
        },
        "auto_apply": False,
    }


def check(source: str | Path, root: str | Path, timeout: float = 10.0) -> dict[str, Any]:
    manifest = load_manifest(source, timeout=timeout)
    result = compare(manifest, root)
    result["source"] = str(source)
    return result
