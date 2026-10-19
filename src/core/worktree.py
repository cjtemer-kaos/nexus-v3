"""git worktree por tarea: aislamiento de cambios (estilo Codex/Cursor).

Cada worktree es una copia de trabajo en una rama nueva, sin tocar el checkout
principal. Útil para que el agente trabaje aislado y luego mergear.
"""
from __future__ import annotations

import os
import subprocess


def _run(args: list[str], cwd: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def repo_root(repo: str) -> str:
    """Raíz del repositorio que contiene `repo` ("" si no hay repo)."""
    rc, out = _run(["rev-parse", "--show-toplevel"], repo, timeout=15)
    if rc != 0 or not out:
        return ""
    return out.strip().replace("\\", "/").rstrip("/")


def _same_path(a: str, b: str) -> bool:
    norm = lambda p: os.path.normcase(os.path.realpath(os.path.abspath(p))).replace("\\", "/").rstrip("/")
    return norm(a) == norm(b)


def is_repo(repo: str) -> bool:
    """True solo si `repo` ES la raíz de su repositorio.

    `rev-parse --is-inside-work-tree` devuelve True para cualquier subcarpeta de
    un repo ancestro (p. ej. C:/ en esta máquina). Aceptar eso haría que se
    creen worktrees en un repo ajeno, así que exigimos coincidencia con la raíz.
    """
    root = repo_root(repo)
    return bool(root) and _same_path(root, repo)


def list_worktrees(repo: str) -> list[dict]:
    rc, out = _run(["worktree", "list", "--porcelain"], repo, timeout=20)
    if rc != 0:
        return []
    items, cur = [], {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                items.append(cur)
            cur = {"path": line[len("worktree "):]}
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].replace("refs/heads/", "")
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line == "bare":
            cur["bare"] = True
    if cur:
        items.append(cur)
    return items


def add_worktree(repo: str, path: str, branch: str, base: str = "HEAD") -> dict:
    if not is_repo(repo):
        parent = repo_root(repo)
        hint = f"; la raíz del repo que lo contiene es {parent}" if parent else ""
        return {"ok": False, "error": f"no es repo git: {repo}{hint}"}
    path = os.path.abspath(path)
    rc, out = _run(["worktree", "add", "-b", branch, path, base], repo, timeout=120)
    return {"ok": rc == 0, "path": path, "branch": branch, "output": out}


def remove_worktree(repo: str, path: str, force: bool = False) -> dict:
    args = ["worktree", "remove", path] + (["--force"] if force else [])
    rc, out = _run(args, repo, timeout=60)
    return {"ok": rc == 0, "output": out}
