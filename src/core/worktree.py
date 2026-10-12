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


def is_repo(repo: str) -> bool:
    rc, _ = _run(["rev-parse", "--is-inside-work-tree"], repo, timeout=15)
    return rc == 0


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
        return {"ok": False, "error": f"no es repo git: {repo}"}
    path = os.path.abspath(path)
    rc, out = _run(["worktree", "add", "-b", branch, path, base], repo, timeout=120)
    return {"ok": rc == 0, "path": path, "branch": branch, "output": out}


def remove_worktree(repo: str, path: str, force: bool = False) -> dict:
    args = ["worktree", "remove", path] + (["--force"] if force else [])
    rc, out = _run(args, repo, timeout=60)
    return {"ok": rc == 0, "output": out}
