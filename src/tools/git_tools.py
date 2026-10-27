"""Tools git con argv (nunca shell): status, diff, log y commit.

Por que existen: el harness solo tenia `exec`, asi que cada tarea git dependia
de que el modelo escribiera el comando exacto (`git -C ... status --porcelain`,
comillas en el mensaje de commit, `--` antes de rutas). Con jail activo varias
de esas formas se bloquean por encadenamiento, y un mensaje con comillas podia
terminar en un commit distinto al pedido. Aca cada operacion se arma como lista
de argumentos: el mensaje viaja como un unico `-m` sin interpretacion y las
rutas van despues de `--`.

Nada de esto hace `pull`/`push`/`reset`: commit local como mucho. El update del
harness sigue siendo de `self_update.py`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

MAX_DIFF_CHARS = 40_000
MAX_LOG = 50


class GitError(RuntimeError):
    """Error de git con el stderr real (no un 'fallo' seco)."""


def _env() -> dict[str, str]:
    """Entorno sin prompts interactivos ni credenciales colgadas."""
    env = {**os.environ}
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "echo",
        "GCM_INTERACTIVE": "never",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    })
    return env


def _run(root: Path, *args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-c", "color.ui=false", *args],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=_env(), creationflags=creation,
        )
    except FileNotFoundError as e:
        raise GitError("git no esta instalado o no esta en PATH") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {args[0]} tardó más de {timeout:.0f}s") from e
    return p.returncode, p.stdout or "", p.stderr or ""


def repo_root(path: str | Path = ".") -> Path:
    """Raíz del repo que contiene `path`. Falla claro si no es un repo."""
    start = Path(path).resolve()
    # Un path inexistente reventaba adentro de `subprocess.Popen` con
    # NotADirectoryError ("El nombre del directorio no es valido"): el modelo
    # leia eso como bug del harness y no como "esa ruta no existe".
    if not start.exists():
        raise GitError(f"no existe la ruta: {start}")
    if start.is_file():
        start = start.parent
    if not start.is_dir():
        raise GitError(f"no es un directorio: {start}")
    code, out, err = _run(start, "rev-parse", "--show-toplevel")
    if code != 0:
        raise GitError(f"no es un repositorio git: {start} ({err.strip() or out.strip()})")
    return Path(out.strip()).resolve()


def _split_z(raw: str) -> list[str]:
    return [ln for ln in raw.split("\0") if ln]


def status(path: str | Path = ".", limit: int = 200) -> dict[str, Any]:
    """Estado real del repo: rama, upstream, ahead/behind y archivos por grupo.

    Usa `--porcelain=v1 -z` (rutas con espacios y renombres no se rompen) y
    separa staged de unstaged, que es lo que el modelo necesita para decidir
    si commitea o no.
    """
    root = repo_root(path)
    code, out, err = _run(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if code != 0:
        raise GitError(f"git status falló: {err.strip()}")
    staged: list[dict[str, str]] = []
    unstaged: list[dict[str, str]] = []
    untracked: list[str] = []
    conflicted: list[str] = []
    entries = _split_z(out)
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 3:
            continue
        x, y, rest = e[0], e[1], e[3:]
        if x in ("R", "C") and i < len(entries):
            nuevo = entries[i]
            i += 1
            rest = f"{rest} -> {nuevo}"
        if x == "?" and y == "?":
            untracked.append(rest)
            continue
        if x in ("U", "A") and y in ("U", "D", "A") or (x == "D" and y == "D"):
            conflicted.append(rest)
        if x not in (" ", "?"):
            staged.append({"code": x, "path": rest})
        if y not in (" ", "?"):
            unstaged.append({"code": y, "path": rest})

    branch = ""
    upstream = ""
    ahead = behind = 0
    c, o, _ = _run(root, "rev-parse", "--abbrev-ref", "HEAD")
    if c == 0:
        branch = o.strip()
    c, o, _ = _run(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if c == 0:
        upstream = o.strip()
        c2, o2, _ = _run(root, "rev-list", "--left-right", "--count", f"{upstream}...HEAD")
        if c2 == 0 and o2.strip():
            parts = o2.split()
            if len(parts) == 2:
                behind, ahead = int(parts[0]), int(parts[1])
    head_code, head_out, _ = _run(root, "rev-parse", "--short", "HEAD")
    return {
        "root": str(root),
        "branch": branch,
        "head": head_out.strip() if head_code == 0 else "",
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "staged": staged[:limit],
        "unstaged": unstaged[:limit],
        "untracked": untracked[:limit],
        "conflicted": conflicted[:limit],
        "clean": not (staged or unstaged or untracked or conflicted),
        "truncated": max(len(staged), len(unstaged), len(untracked)) > limit,
    }


def diff(path: str | Path = ".", staged: bool = False, stat: bool = False,
         paths: list[str] | None = None, max_chars: int = MAX_DIFF_CHARS) -> dict[str, Any]:
    """Diff de trabajo (o `--staged`), opcionalmente acotado a rutas."""
    root = repo_root(path)
    args = ["diff", "--no-color"]
    if staged:
        args.append("--staged")
    if stat:
        args.append("--stat")
    if paths:
        args.extend(["--", *paths])
    code, out, err = _run(root, *args, timeout=60.0)
    if code != 0:
        raise GitError(f"git diff falló: {err.strip()}")
    truncado = len(out) > max_chars
    body = out[:max_chars]
    if truncado:
        body += f"\n… [diff recortado: {len(out)} chars, se muestran {max_chars}]"
    return {
        "root": str(root),
        "staged": staged,
        "stat": stat,
        "empty": not out.strip(),
        "paths": paths or [],
        "diff": body,
    }


def log(path: str | Path = ".", limit: int = 10, paths: list[str] | None = None) -> dict[str, Any]:
    """Ultimos commits (sha corto, fecha, autor, asunto)."""
    root = repo_root(path)
    n = max(1, min(MAX_LOG, int(limit or 10)))
    sep = "\x1f"
    args = ["log", f"-{n}", f"--pretty=format:%h{sep}%ad{sep}%an{sep}%s", "--date=short"]
    if paths:
        args.extend(["--", *paths])
    code, out, err = _run(root, *args, timeout=30.0)
    if code != 0:
        # Repo sin commits: es un estado valido, no un error de git.
        if "does not have any commits" in (err or "") or "unknown revision" in (err or ""):
            return {"root": str(root), "commits": [], "empty": True}
        raise GitError(f"git log falló: {err.strip()}")
    commits = []
    for line in out.splitlines():
        parts = line.split(sep)
        if len(parts) == 4:
            commits.append({"sha": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]})
    return {"root": str(root), "commits": commits, "empty": not commits}


def commit(path: str | Path = ".", message: str = "", all: bool = False,
           paths: list[str] | None = None) -> dict[str, Any]:
    """`git add` + `git commit` local. Nunca pushea ni modifica historia.

    - `message` vacio o solo espacios: se rechaza (git abortaria igual, pero
      con un mensaje que el modelo suele interpretar como "ya commiteo").
    - `all=True` agrega TODO el arbol; con `paths` agrega solo esas rutas.
      Si no se pasa ninguno de los dos, commitea lo que ya estaba staged.
    - Si no hay nada que commitear devuelve `committed: false` con el motivo,
      sin inventar un commit vacio.
    """
    root = repo_root(path)
    msg = (message or "").strip()
    if not msg:
        raise GitError("mensaje de commit requerido (no se permiten commits vacios de mensaje)")
    added: list[str] = []
    if all:
        code, out, err = _run(root, "add", "-A")
        if code != 0:
            raise GitError(f"git add -A falló: {err.strip()}")
        added = ["<todo el arbol>"]
    elif paths:
        code, out, err = _run(root, "add", "--", *paths)
        if code != 0:
            raise GitError(f"git add falló: {err.strip()}")
        added = list(paths)

    # Sin nada en el index se responde ANTES de llamar a `git commit`: en un
    # repo sin identidad configurada, git aborta por "Author identity unknown"
    # aunque no haya cambios, y el modelo terminaba leyendo un error de autor
    # cuando la verdad era "no habia nada para commitear".
    code, _, err = _run(root, "diff", "--cached", "--quiet")
    if code == 0:
        return {
            "root": str(root), "committed": False, "added": added,
            "reason": "no hay cambios para commitear",
        }

    code, out, err = _run(root, "commit", "-m", msg)
    if code != 0:
        blob = f"{out}\n{err}".lower()
        if "nothing to commit" in blob or "no changes added to commit" in blob:
            return {
                "root": str(root), "committed": False, "added": added,
                "reason": "no hay cambios para commitear",
            }
        if ("author identity unknown" in blob or "please tell me who you are" in blob
                or "unable to auto-detect email" in blob):
            raise GitError(
                "git no sabe quien firma: configurá la identidad y volvé a intentar "
                "(`git config --global user.name \"Tu Nombre\"` y "
                "`git config --global user.email \"tu@email\"`, o los mismos comandos "
                "sin --global dentro del repo). No commiteé nada.")
        raise GitError(f"git commit falló: {err.strip() or out.strip()}")
    sha_code, sha_out, _ = _run(root, "rev-parse", "--short", "HEAD")
    show_code, show_out, _ = _run(
        root, "show", "--stat", "--oneline", "--no-color", "HEAD")
    return {
        "root": str(root),
        "committed": True,
        "sha": sha_out.strip() if sha_code == 0 else "",
        "added": added,
        "summary": show_out if show_code == 0 else out.strip(),
    }
