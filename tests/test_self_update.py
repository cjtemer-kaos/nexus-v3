"""Auto-actualizacion por git: pull ff-only, builds y negativas seguras.

Los tests arman repos de verdad en un directorio temporal (origen + clon) porque
lo que se quiere fijar es el comportamiento frente a git real: rama atrasada,
arbol sucio, commits locales y remoto inexistente.
"""
from __future__ import annotations

import subprocess
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
