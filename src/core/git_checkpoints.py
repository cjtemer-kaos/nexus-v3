"""Checkpoints git con untracked, en un repo SOMBRA separado.

El checkpoint por archivo (`checkpoints.py`) solo puede restaurar archivos que
ya existian: no captura archivos nuevos/untracked ni el estado global. Cline
resuelve esto con un repo git shadow. Aca se replica sin tocar el repo real:

- el indice y los objetos viven en `data/git_checkpoints/<sesion>` (o
  `NEXUS_GIT_CHECKPOINTS_DIR`);
- el work-tree apunta a la raiz del proyecto;
- `git add -A` captura tracked + untracked (respetando `.gitignore` y una lista
  de exclusiones para `data/`, `node_modules/`, venvs, builds y DBs);
- restaurar es `git checkout <commit> -- .`; `clean=true` ademas borra los
  archivos que no existian en el checkpoint (nunca toca lo excluido).

Nunca hace commit en el repo del usuario ni modifica su indice.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .environment import data_home, workspace_root
from .text_limits import truncate_head_tail

_EXCLUDES = (
    ".git", ".git/",
    "data/", "brain/", "node_modules/", "vendor/",
    ".venv/", ".venv-*/", "venv/", "env/",
    "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
    "dist/", "build/", "out/", "target/", ".next/", ".nuxt/",
    "*.db", "*.db-*", "*.sqlite", "*.sqlite3", "*.log",
    "*.pyc", "*.pyo", "*.zip", "*.7z", "*.iso",
    "*.mp4", "*.mkv", "*.webm", "*.wav", "*.flac", "*.png", "*.jpg", "*.jpeg",
    "*.gguf", "*.safetensors", "*.ckpt", "*.bin", "*.onnx",
)

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _git() -> str | None:
    return shutil.which("git")


def _checkpoints_root() -> Path:
    raw = os.environ.get("NEXUS_GIT_CHECKPOINTS_DIR")
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (workspace_root() / p)
    return data_home() / "git_checkpoints"


def _session_key(session_id: str) -> str:
    raw = session_id or "default"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._")[:40] or "default"
    return f"{safe}-{hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:8]}"


def _lock_for(gitdir: Path) -> threading.Lock:
    key = str(gitdir)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _run(args: list[str], *, cwd: Path | None = None, timeout: float = 120.0,
         env: dict | None = None) -> subprocess.CompletedProcess:
    git = _git()
    if not git:
        raise RuntimeError("git no esta disponible en PATH")
    proc = subprocess.run(
        [git, "-c", "core.quotepath=false", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args[:3])} fallo: {detail[:1000]}")
    return proc


def _base(gitdir: Path, root: Path) -> list[str]:
    return ["--git-dir", str(gitdir), "--work-tree", str(root)]


def _ensure_repo(session_id: str, root: Path) -> Path:
    gitdir = _checkpoints_root() / _session_key(session_id)
    if (gitdir / "HEAD").exists():
        return gitdir
    gitdir.mkdir(parents=True, exist_ok=True)
    _run(["init", "--quiet", "--bare", str(gitdir)])
    _run(_base(gitdir, root) + ["config", "core.bare", "false"])
    _run(_base(gitdir, root) + ["config", "user.name", "nexus-checkpoints"])
    _run(_base(gitdir, root) + ["config", "user.email", "checkpoints@nexus.local"])
    excludes = list(_EXCLUDES)
    try:
        rel = gitdir.resolve().relative_to(root.resolve())
        excludes.append(rel.as_posix().rstrip("/") + "/")
    except Exception:
        pass
    info = gitdir / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "exclude").write_text("\n".join(excludes) + "\n", encoding="utf-8")
    return gitdir


def _checkpoint_env() -> dict:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "nexus-checkpoints",
        "GIT_AUTHOR_EMAIL": "checkpoints@nexus.local",
        "GIT_COMMITTER_NAME": "nexus-checkpoints",
        "GIT_COMMITTER_EMAIL": "checkpoints@nexus.local",
    })
    return env


def create_git_checkpoint(session_id: str, message: str = "",
                          root: str | Path | None = None) -> dict:
    """Crea un checkpoint global (tracked + untracked) y devuelve su commit."""
    if not _git():
        return {"ok": False, "error": "git no esta disponible"}
    root_p = Path(root).resolve() if root else workspace_root().resolve()
    gitdir = _checkpoints_root() / _session_key(session_id)
    with _lock_for(gitdir):
        try:
            gitdir = _ensure_repo(session_id, root_p)
            base = _base(gitdir, root_p)
            _run(base + ["add", "-A"], cwd=root_p, timeout=180)
            tree = _run(base + ["write-tree"], cwd=root_p).stdout.strip()
            parent = ""
            try:
                parent = _run(base + ["rev-parse", "--verify", "HEAD"], cwd=root_p).stdout.strip()
            except Exception:
                parent = ""
            if parent:
                try:
                    parent_tree = _run(base + ["rev-parse", f"{parent}^{{tree}}"], cwd=root_p).stdout.strip()
                    if parent_tree == tree:
                        return {"ok": True, "id": parent, "changed": False,
                                "message": "sin cambios desde el checkpoint anterior"}
                except Exception:
                    pass
            msg = (message or "").strip() or time.strftime("checkpoint %Y-%m-%d %H:%M:%S")
            args = base + ["commit-tree", tree, "-m", msg]
            if parent:
                args += ["-p", parent]
            commit = _run(args, cwd=root_p, env=_checkpoint_env()).stdout.strip()
            _run(base + ["update-ref", "HEAD", commit], cwd=root_p)
            # `--root` es lo que hace que el primer commit liste sus archivos
            # (sin él, diff-tree no muestra nada del commit raíz y files daba 0).
            names = _run(base + ["diff-tree", "--no-commit-id", "--root", "--name-only", "-r", commit],
                         cwd=root_p).stdout.splitlines()
            return {"ok": True, "id": commit, "changed": True,
                    "files": len([n for n in names if n.strip()]),
                    "message": msg}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def list_git_checkpoints(session_id: str, limit: int = 50,
                         root: str | Path | None = None) -> list[dict]:
    if not _git():
        return []
    root_p = Path(root).resolve() if root else workspace_root().resolve()
    gitdir = _checkpoints_root() / _session_key(session_id)
    if not (gitdir / "HEAD").exists():
        return []
    try:
        out = _run(
            _base(gitdir, root_p) + [
                "log", f"-n{max(1, min(500, int(limit)))}",
                "--format=%H%x1f%ct%x1f%s", "HEAD",
            ],
            cwd=root_p,
        ).stdout
    except Exception:
        return []
    items: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 3:
            continue
        items.append({
            "id": parts[0],
            "ts": int(parts[1] or 0),
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(parts[1] or 0))),
            "message": parts[2],
        })
    return items


def restore_git_checkpoint(session_id: str, checkpoint_id: str,
                           clean: bool = False,
                           root: str | Path | None = None) -> dict:
    """Restaura el work-tree al checkpoint. `clean` borra archivos posteriores."""
    if not _git():
        return {"ok": False, "error": "git no esta disponible"}
    if not checkpoint_id or not re.fullmatch(r"[0-9a-fA-F]{4,64}", checkpoint_id):
        return {"ok": False, "error": "checkpoint_id invalido"}
    root_p = Path(root).resolve() if root else workspace_root().resolve()
    gitdir = _checkpoints_root() / _session_key(session_id)
    if not (gitdir / "HEAD").exists():
        return {"ok": False, "error": "no hay checkpoints para esta sesion"}
    with _lock_for(gitdir):
        base = _base(gitdir, root_p)
        try:
            commit = _run(base + ["rev-parse", "--verify", f"{checkpoint_id}^{{commit}}"],
                          cwd=root_p).stdout.strip()
            _run(base + ["checkout", commit, "--", "."], cwd=root_p, timeout=180)
            cleaned = False
            if clean:
                _run(base + ["read-tree", commit], cwd=root_p)
                _run(base + ["clean", "-fd"], cwd=root_p, timeout=180)
                cleaned = True
            return {"ok": True, "restored": commit, "cleaned": cleaned,
                    "warning": ("clean borro archivos no presentes en el checkpoint "
                                "(las exclusiones se respetan)") if cleaned else ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def diff_git_checkpoint(session_id: str, checkpoint_id: str,
                        root: str | Path | None = None) -> dict:
    if not _git():
        return {"ok": False, "error": "git no esta disponible"}
    root_p = Path(root).resolve() if root else workspace_root().resolve()
    gitdir = _checkpoints_root() / _session_key(session_id)
    if not (gitdir / "HEAD").exists():
        return {"ok": False, "error": "no hay checkpoints para esta sesion"}
    with _lock_for(gitdir):
        base = _base(gitdir, root_p)
        try:
            commit = _run(base + ["rev-parse", "--verify", f"{checkpoint_id}^{{commit}}"],
                          cwd=root_p).stdout.strip()
            _run(base + ["add", "-A"], cwd=root_p, timeout=180)
            out = _run(base + ["diff", "--cached", commit, "--", "."], cwd=root_p).stdout
            return {"ok": True, "id": commit,
                    "diff": truncate_head_tail(out, 48000, label="diff del checkpoint")}
        except Exception as e:
            return {"ok": False, "error": str(e)}
