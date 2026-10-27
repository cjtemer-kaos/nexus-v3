"""Auto-actualizacion por git: pull ff-only, builds y negativas seguras.

Los tests arman repos de verdad en un directorio temporal (origen + clon) porque
lo que se quiere fijar es el comportamiento frente a git real: rama atrasada,
arbol sucio, commits locales y remoto inexistente.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from src.core import self_update as su


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
    )
    assert out.returncode == 0, f"git {args} fallo: {out.stderr}"
    return out.stdout.strip()


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    """(origen, clon) con un commit inicial y un modulo que se puede tocar."""
    monkeypatch.setattr(su, "STATE_PATH", tmp_path / "state" / "self_update.json")
    monkeypatch.setattr(su, "LOG_PATH", tmp_path / "state" / "self_update.log")
    monkeypatch.setattr(su, "LOCK_PATH", tmp_path / "state" / "self_update.lock")

    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "master")
    (origin / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "inicial")

    clone = tmp_path / "clon"
    _git(tmp_path, "clone", str(origin), str(clone))
    return origin, clone


def _nuevo_commit(origen: Path, archivo: str, texto: str) -> None:
    (origen / archivo).parent.mkdir(parents=True, exist_ok=True)
    (origen / archivo).write_text(texto, encoding="utf-8")
    _git(origen, "add", "-A")
    _git(origen, "commit", "-m", f"toca {archivo}")


CFG = {"auto": True, "apply_on_start": True, "git_remote": "origin",
       "git_branch": "master", "build_on_update": False}


def test_sin_repo_no_rompe(tmp_path):
    res = su.check(tmp_path, CFG, fetch=False)
    assert res["is_repo"] is False
    assert res["enabled"] is False
    assert res["update_available"] is False
    # Un directorio sin git jamas se toca.
    assert su.apply(tmp_path, CFG, fetch=False)["applied"] is False


def test_al_dia_no_hace_nada(repos):
    _, clon = repos
    res = su.check(clon, CFG)
    assert res["ok"] is True
    assert res["behind"] == 0
    assert res["update_available"] is False
    assert su.apply(clon, CFG)["applied"] is False


def test_detecta_y_aplica_solo_camino_rapido(repos):
    origen, clon = repos
    _nuevo_commit(origen, "src/core/nuevo.py", "X = 1\n")
    antes = su.check(clon, CFG)
    assert antes["update_available"] is True
    assert antes["behind"] == 1
    assert antes["commits"], "tiene que listar los commits nuevos"

    res = su.apply(clon, CFG)
    assert res["applied"] is True
    assert res["from_head"] != res["to_head"]
    assert "src/core/nuevo.py" in res["changed"]
    # Codigo Python: hace falta que el proceso reinicie para importarlo.
    assert res["restart_required"] is True
    assert (clon / "src" / "core" / "nuevo.py").is_file()
    assert su.check(clon, CFG)["update_available"] is False


def test_solo_ui_no_pide_reinicio_del_backend(repos):
    origen, clon = repos
    _nuevo_commit(origen, "ui/src/App.tsx", "export default 1\n")
    res = su.apply(clon, CFG)
    assert res["applied"] is True
    # Cambio de UI: se recompila, pero el backend Python no cambia.
    assert res["restart_required"] is False


def _dist_nuevo(root: Path, asset: str = "index-nuevo.js") -> None:
    dist = Path(root) / "ui" / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "assets" / asset).write_text("console.log(1)\n", encoding="utf-8")
    (dist / "index.html").write_text(
        f'<script type="module" src="/assets/{asset}"></script>\n', encoding="utf-8")


def test_ui_recompilada_pide_recargar_la_ventana(repos, monkeypatch):
    """Si el build sale bien, el que tiene la ventana abierta tiene que recargarla."""
    origen, clon = repos
    _nuevo_commit(origen, "ui/src/App.tsx", "export default 2\n")

    def fake_npm(root, script, timeout):
        if script == "build:ui":
            _dist_nuevo(root)
        return "ok"

    monkeypatch.setattr(su, "_npm", fake_npm)
    res = su.apply(clon, {**CFG, "build_on_update": True})
    assert res["applied"] is True
    assert res["built"]["ui"] == "ok"
    assert res["stale"] == []
    assert res["ui_stale"] is False
    assert res["reload_required"] is True
    assert res["restart_required"] is False


def test_sin_node_modules_la_ui_queda_marcada_vieja(repos):
    """Sin npm/node_modules el pull entra pero el bundle sigue viejo: hay que decirlo."""
    origen, clon = repos
    _nuevo_commit(origen, "ui/src/App.tsx", "export default 3\n")
    res = su.apply(clon, {**CFG, "build_on_update": True})
    assert res["applied"] is True
    assert res["built"]["ui"].startswith("skip")
    assert res["ui_stale"] is True
    assert "ui" in res["stale"]
    assert res["reload_required"] is False


def test_build_que_no_regenera_el_index_queda_stale(repos, monkeypatch):
    origen, clon = repos
    _nuevo_commit(origen, "ui/src/App.tsx", "export default 4\n")
    # Sale 0 pero no escribio nada: seria el caso de un build a otro directorio.
    monkeypatch.setattr(su, "_npm", lambda root, script, timeout: "ok")
    res = su.apply(clon, {**CFG, "build_on_update": True})
    assert res["applied"] is True
    assert res["ui_stale"] is True
    assert "index.html" in res["built"]["ui"]


def test_needs_build_clasifica_por_carpeta():
    assert su._needs_build(["ui/src/App.tsx"]) == {"ui": True, "electron": False}
    assert su._needs_build(["ui/dist/assets/index.js"]) == {"ui": False, "electron": False}
    assert su._needs_build(["electron/main.ts"]) == {"ui": False, "electron": True}
    assert su._needs_build(["package.json"]) == {"ui": True, "electron": True}
    # Python/config no toca ninguno de los dos bundles.
    assert su._needs_build(["src/api/server.py", "config/config.yaml"]) == {"ui": False, "electron": False}


def test_ui_bundle_problem_detecta_index_viejo_y_assets_faltantes(tmp_path):
    _dist_nuevo(tmp_path, "index-abc.js")
    assert su._ui_bundle_problem(tmp_path, since=0) == ""

    index = tmp_path / "ui" / "dist" / "index.html"
    os.utime(index, (1_000, 1_000))
    assert "no regenero" in su._ui_bundle_problem(tmp_path, since=time.time())

    (tmp_path / "ui" / "dist" / "assets" / "index-abc.js").unlink()
    assert "no existen" in su._ui_bundle_problem(tmp_path, since=0)

    shutil.rmtree(tmp_path / "ui")
    assert "no quedo" in su._ui_bundle_problem(tmp_path, since=0)


def test_arbol_sucio_no_se_toca(repos):
    origen, clon = repos
    _nuevo_commit(origen, "app.py", "print('v2')\n")
    # Trabajo local sin commitear: el pull ff-only podria pisarlo.
    (clon / "app.py").write_text("print('mio')\n", encoding="utf-8")
    res = su.apply(clon, CFG)
    assert res["applied"] is False
    assert "cambio" in (res.get("blocked") or "").lower()
    assert (clon / "app.py").read_text(encoding="utf-8") == "print('mio')\n"


def test_commits_locales_no_se_tocan(repos):
    origen, clon = repos
    _nuevo_commit(origen, "app.py", "print('v2')\n")
    (clon / "local.txt").write_text("solo mio\n", encoding="utf-8")
    _git(clon, "add", "-A")
    _git(clon, "commit", "-m", "commit local")
    res = su.apply(clon, CFG)
    assert res["applied"] is False
    assert "local" in (res.get("blocked") or "").lower()


def test_sin_trackear_no_bloquea_el_update(repos):
    """`.venv/` o `memories.json` sin trackear no impiden el pull.

    En la practica toda PC tiene carpetas asi (entornos, volcados del brain).
    Contarlas como "cambios locales" dejaba el auto-update frenado para siempre.
    """
    origen, clon = repos
    _nuevo_commit(origen, "app.py", "print('v4')\n")
    (clon / ".venv").mkdir()
    (clon / ".venv" / "pyvenv.cfg").write_text("home = C:\\Python\n", encoding="utf-8")
    (clon / "memories.json").write_text("{}\n", encoding="utf-8")

    res = su.apply(clon, CFG)
    assert res["applied"] is True
    assert res["dirty"] == 0
    assert res.get("untracked") == 2
    assert (clon / "app.py").read_text(encoding="utf-8") == "print('v4')\n"
    # Y siguen ahi despues del pull.
    assert (clon / "memories.json").is_file()


def test_rama_inexistente_reporta_en_vez_de_romper(repos):
    _, clon = repos
    cfg = {**CFG, "git_branch": "no-existe"}
    res = su.check(clon, cfg)
    assert res["ok"] is False
    assert "no-existe" in res["reason"]
    assert su.apply(clon, cfg)["applied"] is False


def test_min_age_usa_el_ultimo_chequeo(repos):
    origen, clon = repos
    _nuevo_commit(origen, "app.py", "print('v3')\n")
    primero = su.apply(clon, CFG)  # deja estado guardado
    assert primero["applied"] is True
    cacheado = su.check(clon, CFG, fetch=True, min_age_s=300)
    assert cacheado.get("cached") is True
    assert cacheado["head"] == primero["to_head"]
