"""Checkpoints git con untracked en un repo sombra."""
from __future__ import annotations

import shutil

import pytest

from src.core import git_checkpoints as gc


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")


def test_checkpoint_captures_untracked_and_restores(tmp_path, monkeypatch):
    shadow = tmp_path / "shadow"
    monkeypatch.setenv("NEXUS_GIT_CHECKPOINTS_DIR", str(shadow))
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.txt").write_text("uno", encoding="utf-8")

    first = gc.create_git_checkpoint("s1", "inicial", root=root)
    assert first["ok"] and first["changed"]
    assert not (root / ".git").exists(), "no debe crear repo en el work-tree"

    (root / "a.txt").write_text("dos", encoding="utf-8")
    (root / "nuevo.txt").write_text("untracked", encoding="utf-8")
    second = gc.create_git_checkpoint("s1", "con untracked", root=root)
    assert second["ok"] and second["changed"]
    assert second["id"] != first["id"]

    items = gc.list_git_checkpoints("s1", root=root)
    assert len(items) == 2

    diff = gc.diff_git_checkpoint("s1", first["id"], root=root)
    assert diff["ok"]
    assert "nuevo.txt" in diff["diff"]

    restored = gc.restore_git_checkpoint("s1", first["id"], clean=True, root=root)
    assert restored["ok"] and restored["cleaned"]
    assert (root / "a.txt").read_text(encoding="utf-8") == "uno"
    assert not (root / "nuevo.txt").exists()


def test_checkpoint_without_changes_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_GIT_CHECKPOINTS_DIR", str(tmp_path / "shadow"))
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.txt").write_text("igual", encoding="utf-8")
    first = gc.create_git_checkpoint("s2", "uno", root=root)
    again = gc.create_git_checkpoint("s2", "dos", root=root)
    assert first["ok"] and again["ok"]
    assert again["changed"] is False
    assert again["id"] == first["id"]


def test_restore_rejects_bad_id(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_GIT_CHECKPOINTS_DIR", str(tmp_path / "shadow"))
    root = tmp_path / "work"
    root.mkdir()
    res = gc.restore_git_checkpoint("s3", "../etc/passwd", root=root)
    assert res["ok"] is False


def test_diff_y_restore_con_id_inexistente_no_filtran_el_error_de_git(tmp_path, monkeypatch):
    """Antes el fallo de `rev-parse` llegaba crudo: "fatal: Needed a single
    revision". El modelo no sabe que hacer con eso; tiene que decir que el
    checkpoint no existe y como listar los validos."""
    monkeypatch.setenv("NEXUS_GIT_CHECKPOINTS_DIR", str(tmp_path / "shadow"))
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.txt").write_text("uno", encoding="utf-8")
    primero = gc.create_git_checkpoint("s4", "inicial", root=root)
    assert primero["ok"]

    diff = gc.diff_git_checkpoint("s4", "deadbeef", root=root)
    assert diff["ok"] is False
    assert "no encontrado" in diff["error"]
    assert "fatal" not in diff["error"]
    assert "checkpoint_list" in diff["error"]

    r = gc.restore_git_checkpoint("s4", "deadbeef", root=root)
    assert r["ok"] is False
    assert "no encontrado" in r["error"] and "fatal" not in r["error"]

    malo = gc.diff_git_checkpoint("s4", "", root=root)
    assert malo["ok"] is False and "invalido" in malo["error"]
