"""Tools git por argv: estado, diff, log y commit local.

Los tests arman repos de verdad en tmp_path: lo que se fija es el
comportamiento frente a git real (repo limpio, con cambios, sin repo,
mensaje vacio y "no hay nada que commitear").
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.tools import git_tools as gt


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
    )
    assert out.returncode == 0, f"git {args} fallo: {out.stderr}"
    return out.stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    # Identidad local: sin esto `git commit` aborta por "Author identity
    # unknown" y el test mediria el entorno, no la tool.
    _git(root, "config", "user.email", "test@nexus.local")
    _git(root, "config", "user.name", "Test Nexus")
    (root / "a.txt").write_text("uno\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "inicial")
    return root


def test_status_reporta_rama_limpia(repo):
    st = gt.status(repo)
    assert st["clean"] is True
    assert st["branch"] in ("master", "main")
    assert st["staged"] == [] and st["unstaged"] == [] and st["untracked"] == []


def test_status_separa_staged_unstaged_y_untracked(repo):
    (repo / "a.txt").write_text("dos\n", encoding="utf-8")
    (repo / "nuevo con espacio.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("tres\n", encoding="utf-8")

    st = gt.status(repo)
    assert [f["path"] for f in st["staged"]] == ["a.txt"]
    assert [f["path"] for f in st["unstaged"]] == ["a.txt"]
    # El nombre con espacio sobrevive porque se parsea -z, no lineas.
    assert st["untracked"] == ["nuevo con espacio.txt"]
    assert st["clean"] is False


def test_status_sin_repo_falla_claro(tmp_path, monkeypatch):
    """Directorio fuera de todo repo: GitError con la ruta, no un crash adentro.

    Se simula la respuesta de git a proposito: en esta maquina existe `C:\\.git`,
    asi que cualquier ruta bajo C: ES un repo para git y un tmp_path no sirve
    como 'no-repo'.
    """
    def fake_run(_root, *_args, timeout=30.0):
        return 128, "", "fatal: not a git repository (or any of the parent directories): .git"

    monkeypatch.setattr(gt, "_run", fake_run)
    with pytest.raises(gt.GitError) as e:
        gt.status(tmp_path)
    assert "no es un repositorio" in str(e.value).lower()


def test_status_ruta_inexistente_falla_claro(tmp_path):
    """Ruta que no existe: se dice, no se deja escapar NotADirectoryError."""
    with pytest.raises(gt.GitError) as e:
        gt.status(tmp_path / "no-existe")
    assert "no existe la ruta" in str(e.value).lower()


def test_diff_working_y_staged(repo):
    (repo / "a.txt").write_text("cambiado\n", encoding="utf-8")
    d = gt.diff(repo)
    assert d["empty"] is False
    assert "+cambiado" in d["diff"]
    assert gt.diff(repo, staged=True)["empty"] is True

    _git(repo, "add", "a.txt")
    assert gt.diff(repo, staged=True)["empty"] is False
    # Con todo en el index, el diff de trabajo esta vacio: --stat sin --staged
    # tambien (compara contra el index, no contra HEAD).
    assert gt.diff(repo, stat=True)["diff"].strip() == ""
    assert gt.diff(repo, stat=True, staged=True)["diff"].strip() != ""


def test_diff_recorta_sin_romper(repo):
    (repo / "a.txt").write_text("x" * 5000 + "\n", encoding="utf-8")
    d = gt.diff(repo, max_chars=200)
    assert "recortado" in d["diff"]
    assert len(d["diff"]) < 400


def test_log_devuelve_commits(repo):
    lg = gt.log(repo, limit=5)
    assert lg["empty"] is False
    assert lg["commits"][0]["subject"] == "inicial"
    assert len(lg["commits"][0]["sha"]) >= 7


def test_commit_exige_mensaje(repo):
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    with pytest.raises(gt.GitError):
        gt.commit(repo, message="   ", all=True)
    # No commiteo nada: sigue habiendo un solo commit.
    assert len(gt.log(repo)["commits"]) == 1


def test_commit_agrega_y_devuelve_sha(repo):
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    res = gt.commit(repo, message="agrega b", all=True)
    assert res["committed"] is True
    assert res["sha"]
    assert gt.status(repo)["clean"] is True
    assert gt.log(repo)["commits"][0]["subject"] == "agrega b"


def test_commit_sin_cambios_no_inventa_nada(repo):
    res = gt.commit(repo, message="vacio", all=True)
    assert res["committed"] is False
    assert "no hay cambios" in res["reason"].lower()
    assert len(gt.log(repo)["commits"]) == 1


def test_commit_con_mensaje_que_parece_flag_no_se_interpreta(repo):
    """El mensaje va como `-m` unico: un texto con `--` no se vuelve argumento."""
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    msg = "arregla --force y comillas 'raras'"
    res = gt.commit(repo, message=msg, all=True)
    assert res["committed"] is True
    assert gt.log(repo)["commits"][0]["subject"] == msg


def test_commit_sin_identidad_lo_dice_claro(tmp_path):
    """Repo sin user.name/email: el error nombra la causa y no commitea nada."""
    root = tmp_path / "sin-id"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "z.txt").write_text("z\n", encoding="utf-8")
    with pytest.raises(gt.GitError) as e:
        gt.commit(root, message="intento", all=True)
    msg = str(e.value).lower()
    assert "identidad" in msg
    assert "user.email" in msg
    # No invento un commit: sigue habiendo cambios staged.
    assert gt.status(root)["clean"] is False


def test_commit_sin_cambios_no_culpa_a_la_identidad(tmp_path):
    """Sin cambios se responde 'no hay cambios', aunque falte la identidad."""
    root = tmp_path / "vacio"
    root.mkdir()
    _git(root, "init", "-q")
    res = gt.commit(root, message="nada", all=True)
    assert res["committed"] is False
    assert "no hay cambios" in res["reason"].lower()
