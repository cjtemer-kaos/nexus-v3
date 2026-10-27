"""Auto-actualizacion del codigo desde git: silenciosa y con red de seguridad.

Por que existe: el repo cambia seguido (esta sola sesion movio el harness de un
turno de 18 s a 3,5 s). Si cada maquina tiene que abrir una consola y correr
`git pull`, en la practica quedan versiones viejas dando vueltas y los bugs ya
arreglados siguen apareciendo. Esto hace el pull solo, ANTES de que la app abra:
cuando la ventana aparece, ya es la version nueva.

Reglas (si algo no cuadra, NO se toca nada y la app arranca igual):

- `git pull --ff-only`: si el remoto divergio o hay commits locales, no se toca.
- Archivos versionados con cambios sin commitear: no se toca (puede ser trabajo
  de alguien). Los archivos SIN trackear (`.venv/`, `brain/memories.json`,
  volcados de base) no bloquean: existen asi en todas las PC y no los pisa un pull.
- Sin repo git (instalacion por zip/release): se reporta y se sigue.
- Solo se recompila lo que cambio (`ui/dist`, `electron/dist`) y solo si hay npm.
- Nunca se tocan datos: `data/`, `brain/`, `workspace/`, `.venv*` estan ignorados.
- Todo oculto: `CREATE_NO_WINDOW` + timeout en cada comando. Nada de consolas.

Uso:
    python -m src.core.self_update --check          # estado (JSON)
    python -m src.core.self_update --apply-on-start # lo que corre el lanzador
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Raiz del proyecto: este archivo vive en <root>/src/core/.
ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "data" / "self_update.log"
STATE_PATH = ROOT / "data" / "self_update.json"
LOCK_PATH = ROOT / "data" / "self_update.lock"
LOG_MAX_BYTES = 512 * 1024
LOCK_STALE_S = 900

DEFAULTS: dict[str, Any] = {
    "auto": True,            # interruptor general
    "apply_on_start": True,  # pull al abrir la app / levantar el servicio
    "git_remote": "origin",
    "git_branch": "",        # vacio = la rama actual
    "check_interval_s": 21600,
    "build_on_update": True,
    "fetch_timeout_s": 25,
    "pull_timeout_s": 180,
    "build_timeout_s": 300,
}

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(msg: str) -> None:
    """Una linea por evento en data/self_update.log (con rotacion simple).

    El lanzador corre esto oculto: sin este archivo, un pull que falla no deja
    rastro y el usuario no tiene como saber por que la app no se actualizo.
    """
    line = f"{_now_iso()} {msg}\n"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            if LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                backup = LOG_PATH.with_suffix(".log.1")
                backup.unlink(missing_ok=True)
                LOG_PATH.replace(backup)
        except OSError:
            pass
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _run(cmd: list[str], cwd: Path, timeout: float, env: dict | None = None) -> tuple[int, str]:
    """Ejecuta un comando oculto y devuelve (codigo, salida combinada)."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            env=full_env, creationflags=_NO_WINDOW,
        )
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 124, f"timeout tras {timeout}s: {' '.join(cmd[:3])}"
    except FileNotFoundError:
        return 127, f"no existe el ejecutable: {cmd[0]}"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def _git(root: Path, *args: str, timeout: float = 25) -> tuple[int, str, str]:
    code, out = _run(["git", *args], root, timeout, env={
        # Sin esto, un remoto que pide credenciales deja el lanzador colgado
        # esperando un prompt invisible hasta el timeout.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "echo",
        "GCM_INTERACTIVE": "never",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
    })
    return code, out, out


def config(root: Path | None = None) -> dict[str, Any]:
    """Bloque `updates` de config.yaml mezclado con DEFAULTS."""
    cfg: dict[str, Any] = {}
    try:
        from src.core.config import load_raw_config

        raw = load_raw_config() or {}
        cfg = dict(raw.get("updates") or {})
    except Exception as e:
        _log(f"config: no se pudo leer ({e}); uso defaults")
    out = dict(DEFAULTS)
    for k, v in cfg.items():
        if v is not None:
            out[k] = v
    return out


def repo_state(root: Path = ROOT) -> dict[str, Any]:
    """Estado del repo, exigiendo que el proyecto sea LA RAIZ del repo.

    En esta PC hay un `.git` en `C:\\`, asi que cualquier carpeta de ahi "parece"
    un repo y un `git pull` terminaria actualizando algo que no es la app. Si la
    raiz del repo no es la raiz del proyecto, se trata como "no es repo".
    """
    code, out, _ = _git(root, "rev-parse", "--is-inside-work-tree", timeout=10)
    if code != 0 or out.strip() != "true":
        return {"is_repo": False, "branch": "", "head": "", "dirty": 0,
                "dirty_files": [], "untracked": 0, "untracked_files": [],
                "reason": "no es un repo git"}
    _, top, _ = _git(root, "rev-parse", "--show-toplevel", timeout=10)
    top_raw = top.strip()
    if not top_raw:
        return {"is_repo": False, "branch": "", "head": "", "dirty": 0,
                "dirty_files": [], "untracked": 0, "untracked_files": [],
                "reason": "git no reporto la raiz del repo"}
    try:
        same = os.path.normcase(str(Path(top_raw).resolve())) == os.path.normcase(str(Path(root).resolve()))
    except OSError:
        same = False
    if not same:
        return {"is_repo": False, "branch": "", "head": "", "dirty": 0, "dirty_files": [],
                "untracked": 0, "untracked_files": [], "repo_root": top_raw,
                "reason": f"el proyecto no es la raiz del repo (el .git esta en {top_raw})"}
    _, branch, _ = _git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    _, head, _ = _git(root, "rev-parse", "--short", "HEAD", timeout=10)
    _, porcelain, _ = _git(root, "status", "--porcelain", timeout=20)
    files = [ln for ln in porcelain.splitlines() if ln.strip()]
    # Solo lo que git va a pisar con el pull: archivos versionados modificados
    # o en el index. Los "??" (sin trackear) NO bloquean nada: un `.venv/`,
    # `brain/memories.json` o un volcado de base viven asi en todas las PC, y
    # contarlos como "cambios locales" dejaba el auto-update frenado de por vida.
    # Si un pull chocara con uno de ellos, git aborta solo y avisa.
    modificados = [ln for ln in files if not ln.startswith("??")]
    sueltos = [ln[3:].strip() for ln in files if ln.startswith("??")]
    return {
        "is_repo": True,
        "branch": branch.strip(),
        "head": head.strip(),
        "dirty": len(modificados),
        "dirty_files": modificados[:20],
        "untracked": len(sueltos),
        "untracked_files": sueltos[:20],
    }


def _counts(root: Path, remote_ref: str) -> dict[str, int]:
    """(adelante, atras) respecto del remoto: `rev-list --left-right --count`."""
    code, out, _ = _git(root, "rev-list", "--left-right", "--count", f"HEAD...{remote_ref}", timeout=20)
    if code != 0:
        return {"ahead": 0, "behind": 0}
    parts = out.replace("\t", " ").split()
    try:
        return {"ahead": int(parts[0]), "behind": int(parts[1])}
    except (IndexError, ValueError):
        return {"ahead": 0, "behind": 0}


def _commits(root: Path, remote_ref: str, limit: int = 10) -> list[str]:
    code, out, _ = _git(root, "log", "--oneline", "--no-decorate", "-n", str(limit),
                        f"HEAD..{remote_ref}", timeout=20)
    if code != 0 or not out.strip():
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def check(root: Path = ROOT, cfg: dict | None = None, fetch: bool = True,
          min_age_s: float = 0) -> dict[str, Any]:
    """Estado respecto del remoto. Solo lectura salvo el `git fetch`.

    `min_age_s` evita el doble trabajo tipico del arranque: el lanzador .vbs ya
    hizo el pull y dos segundos despues Electron volveria a pedir lo mismo. Si
    el ultimo chequeo es mas nuevo que eso, se devuelve cacheado.
    """
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)
    if fetch and min_age_s > 0:
        try:
            age = time.time() - STATE_PATH.stat().st_mtime
            cached = (last_state() or {}).get("last_check") or {}
            if age < min_age_s and cached.get("ok"):
                return {**cached, "cached": True}
        except OSError:
            pass
    res: dict[str, Any] = {
        "ok": True, "enabled": bool(c.get("auto", True)), "checked_at": _now_iso(),
        "update_available": False, "applied": False, "blocked": "", "commits": [],
    }
    st = repo_state(root)
    res.update(st)
    if not st["is_repo"]:
        res.update(enabled=False,
                   reason=st.get("reason") or "no es un repo git (instalacion por zip/release)")
        return res
    if not res["enabled"]:
        res["reason"] = "updates.auto desactivado"
        return res

    remote = str(c.get("git_remote") or "origin")
    branch = str(c.get("git_branch") or "").strip() or st["branch"] or "master"
    remote_ref = f"{remote}/{branch}"
    res["remote"] = remote_ref
    res["auto"] = {
        "apply_on_start": bool(c.get("apply_on_start", True)),
        "check_interval_s": int(c.get("check_interval_s") or 0),
        "build_on_update": bool(c.get("build_on_update", True)),
    }
    if fetch:
        code, out, _ = _git(root, "fetch", "--quiet", "--no-tags", remote, branch,
                            timeout=float(c.get("fetch_timeout_s") or 25))
        if code != 0:
            res.update(ok=False, reason=f"git fetch fallo: {out[:200]}")
            return res
    _, remote_head, _ = _git(root, "rev-parse", "--short", remote_ref, timeout=10)
    res["remote_head"] = remote_head.strip()
    if not res["remote_head"]:
        res.update(ok=False, reason=f"no existe {remote_ref} (revisa updates.git_remote/git_branch)")
        return res
    counts = _counts(root, remote_ref)
    res.update(counts)
    res["update_available"] = counts["behind"] > 0
    res["commits"] = _commits(root, remote_ref) if res["update_available"] else []
    if st["dirty"]:
        res["blocked"] = f"hay {st['dirty']} cambio(s) locales sin commitear"
    elif counts["ahead"]:
        res["blocked"] = f"hay {counts['ahead']} commit(s) locales sin publicar"
    return res


def _changed_files(root: Path, old: str, new: str) -> list[str]:
    code, out, _ = _git(root, "diff", "--name-only", f"{old}..{new}", timeout=30)
    if code != 0 or not out.strip():
        return []
    return [ln.strip().replace("\\", "/") for ln in out.splitlines() if ln.strip()]


# Archivos de configuracion que cambian el bundler aunque no vivan en ui/.
_BUILD_CONFIG_FILES = {
    "tsconfig.json", "vite.config.ts", "vite.config.js", "tailwind.config.js",
    "tailwind.config.ts", "postcss.config.js", "postcss.config.cjs",
}


def _needs_build(changed: list[str]) -> dict[str, bool]:
    """Que hay que recompilar para que el cambio se vea de verdad.

    `ui/dist` es un artefacto: si el pull trae `ui/src/…` nuevo y nadie corre
    Vite, la ventana sigue sirviendo el bundle viejo. Por eso cualquier archivo
    bajo `ui/` (menos `ui/dist/`) exige rebuild de la UI.
    """
    ui = False
    electron = False
    for p in changed:
        if p.startswith("ui/") and not p.startswith("ui/dist/"):
            ui = True
        elif p.startswith("electron/") and not p.startswith("electron/dist/"):
            electron = True
        elif p in ("package.json", "package-lock.json") or p in _BUILD_CONFIG_FILES \
                or p.startswith(("electron/tsconfig", "ui/tsconfig")):
            ui = electron = True
    return {"ui": ui, "electron": electron}


def _ui_bundle_problem(root: Path, since: float) -> str:
    """Verifica que el build haya dejado un bundle nuevo y coherente.

    Un `vite build` que devuelve 0 pero escribe en otro lado (o un index.html
    viejo apuntando a un asset que ya no existe) deja la ventana en blanco y
    nadie se entera. Devuelve "" si esta todo bien.
    """
    import re

    index = root / "ui" / "dist" / "index.html"
    if not index.is_file():
        return "no quedo ui/dist/index.html"
    try:
        if index.stat().st_mtime + 5 < since:
            return "el build no regenero ui/dist/index.html"
        html = index.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"no se pudo leer ui/dist/index.html: {e}"
    refs = re.findall(r'(?:src|href)="(/?assets/[^"]+)"', html)
    faltan = [r for r in refs if not (root / "ui" / "dist" / r.lstrip("/")).is_file()]
    if faltan:
        return f"index.html referencia assets que no existen: {', '.join(faltan[:3])}"
    return ""


def _npm(root: Path, script: str, timeout: float) -> str:
    """Corre `npm run <script>` oculto. Devuelve ok|skip|error:detalle."""
    if not (root / "node_modules").is_dir():
        return "skip:sin node_modules"
    npm = shutil.which("npm") or ("npm.cmd" if os.name == "nt" else "")
    if not npm:
        return "skip:sin npm"
    cmd = ["cmd", "/c", npm, "run", script] if os.name == "nt" else [npm, "run", script]
    code, out = _run(cmd, root, timeout)
    if code == 0:
        return "ok"
    _log(f"build {script} fallo (code={code}): {out[-400:]}")
    return f"error:{out[-200:]}"


def _lock() -> bool:
    """Evita que la app y el servicio de :9000 hagan pull a la vez."""
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOCK_PATH.exists():
            age = time.time() - LOCK_PATH.stat().st_mtime
            if age < LOCK_STALE_S:
                return False
            LOCK_PATH.unlink(missing_ok=True)
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def _unlock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def save_state(payload: dict[str, Any]) -> None:
    """Ultimo resultado, para mostrarlo en la UI sin volver a sondear."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def last_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply(root: Path = ROOT, cfg: dict | None = None, fetch: bool = True) -> dict[str, Any]:
    """Pull ff-only si corresponde, recompila lo necesario y reporta el resto.

    Nunca lanza: cualquier problema vuelve en `ok=False` + `reason` y la app
    arranca con el codigo que ya tiene.
    """
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)
    res = check(root, c, fetch=fetch)
    res["applied"] = False
    if not res.get("ok") or not res.get("update_available"):
        if res.get("ok"):
            res["reason"] = res.get("reason") or "al dia"
        save_state({**last_state(), "last_check": res})
        return res
    if res.get("blocked"):
        res["reason"] = res["blocked"]
        _log(f"skip: {res['reason']}")
        save_state({**last_state(), "last_check": res})
        return res
    if not _lock():
        res.update(ok=True, reason="otra instancia esta actualizando")
        return res

    try:
        old_head = res.get("head") or ""
        remote = str(c.get("git_remote") or "origin")
        branch = str(c.get("git_branch") or "").strip() or res.get("branch") or "master"
        pull_ts = time.time()
        code, out = _run(["git", "pull", "--ff-only", "--no-rebase", "--no-tags", remote, branch],
                         root, float(c.get("pull_timeout_s") or 180), env={
                             "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo",
                             "GCM_INTERACTIVE": "never",
                             "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
                         })
        if code != 0:
            res.update(ok=False, reason=f"git pull fallo: {out[-300:]}")
            _log(f"pull fallo (code={code}): {out[-400:]}")
            save_state({**last_state(), "last_check": res})
            return res

        st = repo_state(root)
        new_head = st.get("head") or ""
        changed = _changed_files(root, old_head, new_head) if old_head and new_head else []
        built: dict[str, str] = {}
        stale: list[str] = []
        need = _needs_build(changed)
        if c.get("build_on_update", True):
            if need["electron"]:
                built["electron"] = _npm(root, "build:electron", float(c.get("build_timeout_s") or 300))
                if built["electron"] != "ok":
                    stale.append("electron")
            if need["ui"]:
                built["ui"] = _npm(root, "build:ui", float(c.get("build_timeout_s") or 300))
                if built["ui"] != "ok":
                    stale.append("ui")
                else:
                    problema = _ui_bundle_problem(root, since=pull_ts)
                    if problema:
                        built["ui"] = f"error:{problema}"
                        stale.append("ui")
        elif need["ui"] or need["electron"]:
            # build_on_update apagado: el codigo nuevo esta en disco pero el
            # bundle que sirve Electron sigue siendo el viejo.
            stale.extend([k for k, v in need.items() if v])

        # Codigo Python/config no necesita compilar: necesita que el proceso
        # vuelva a arrancar para importar los modulos nuevos.
        restart = any(
            p.startswith(("src/", "config/", "scripts/")) or p.endswith((".py", ".yaml", ".yml"))
            for p in changed
        )
        res.update(
            applied=True, ok=True, reason="", from_head=old_head, to_head=new_head,
            head=new_head, changed=changed[:200], changed_count=len(changed),
            built=built, needs_build=need, stale=stale, ui_stale="ui" in stale,
            reload_required="ui" in built and built.get("ui") == "ok",
            restart_required=restart, applied_at=_now_iso(),
            update_available=False, behind=0, commits=[],
        )
        if stale:
            _log("OJO: quedo sin recompilar " + ", ".join(stale)
                 + f" (builds={built}); la app va a seguir mostrando la version vieja")
        _log(f"aplicado {old_head}->{new_head}: {len(changed)} archivo(s)"
             + (f" builds={built}" if built else "") + ("; requiere reinicio" if restart else ""))
        save_state({**last_state(), "last_check": res})
        return res
    finally:
        _unlock()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = ROOT
    if "--root" in args:
        i = args.index("--root")
        try:
            root = Path(args[i + 1]).resolve()
            del args[i:i + 2]
        except IndexError:
            pass
    do_apply = "--apply-on-start" in args or "--apply" in args
    fetch = "--no-fetch" not in args
    quiet = "--quiet" in args
    min_age = 0.0
    if "--min-age" in args:
        i = args.index("--min-age")
        try:
            min_age = float(args[i + 1])
            del args[i:i + 2]
        except (IndexError, ValueError):
            min_age = 0.0
    c = config(root)
    if do_apply:
        cached = check(root, c, fetch=True, min_age_s=min_age) if min_age else {}
        res = cached if cached.get("cached") else apply(root, c, fetch=fetch)
    else:
        res = check(root, c, fetch=fetch, min_age_s=min_age)
    if "--json" in args or (do_apply and not quiet):
        print(json.dumps(res, ensure_ascii=False))
    elif not quiet:
        if res.get("applied"):
            print(f"actualizado {res.get('from_head')} -> {res.get('to_head')}"
                  f" ({res.get('changed_count')} archivos)")
            if res.get("stale"):
                print("OJO: quedo sin recompilar " + ", ".join(res["stale"])
                      + " -> la app sigue con la version vieja (ver data/self_update.log)")
        elif res.get("update_available"):
            print(f"hay {res.get('behind')} commit(s) nuevos ({res.get('blocked') or 'listo para aplicar'})")
        elif res.get("ok"):
            print(f"al dia ({res.get('head')})")
        else:
            print(f"sin actualizar: {res.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
