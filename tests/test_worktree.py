"""Tests del aislamiento por git worktree (sub-agentes y tareas paralelas)."""
import os
import subprocess

import pytest

from src.core.worktree import add_worktree, is_repo, list_worktrees, remove_worktree


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@nexus.local")
    _git(root, "config", "user.name", "Nexus Tests")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "base.txt")
    _git(root, "commit", "-m", "base")
    return str(root)


def test_is_repo(repo, tmp_path):
    assert is_repo(repo) is True
    assert is_repo(str(tmp_path)) is False


def test_subdirectory_of_repo_is_not_accepted(repo):
    sub = os.path.join(repo, "src")
    os.makedirs(sub)
    assert is_repo(sub) is False
    res = add_worktree(sub, os.path.join(repo, "wt-sub"), "nexus/sub")
    assert res["ok"] is False
    assert "no es repo git" in res["error"]
    assert "la raíz del repo que lo contiene" in res["error"]


def test_add_lists_and_remove(repo, tmp_path):
    target = tmp_path / "wt1"
    res = add_worktree(repo, str(target), "nexus/wt1")
    assert res["ok"] is True, res.get("output")
    assert (target / "base.txt").exists()

    listed = list_worktrees(repo)
    paths = [w["path"].replace("\\", "/") for w in listed]
    assert any(p.endswith("wt1") for p in paths)
    branches = [w.get("branch", "") for w in listed]
    assert "nexus/wt1" in branches

    removed = remove_worktree(repo, str(target))
    assert removed["ok"] is True, removed.get("output")
    assert not target.exists()
    assert len(list_worktrees(repo)) == 1


def test_add_on_non_repo_fails(tmp_path):
    res = add_worktree(str(tmp_path), str(tmp_path / "wt"), "nexus/x")
    assert res["ok"] is False
    assert "no es repo git" in res["error"]


def test_duplicate_branch_fails(repo, tmp_path):
    target = tmp_path / "wt1"
    assert add_worktree(repo, str(target), "nexus/dup")["ok"] is True
    second = add_worktree(repo, str(tmp_path / "wt2"), "nexus/dup")
    assert second["ok"] is False


def test_remove_dirty_worktree_requires_force(repo, tmp_path):
    target = tmp_path / "wt1"
    assert add_worktree(repo, str(target), "nexus/dirty")["ok"] is True
    (target / "nuevo.txt").write_text("sin commitear\n", encoding="utf-8")

    blocked = remove_worktree(repo, str(target))
    assert blocked["ok"] is False

    forced = remove_worktree(repo, str(target), force=True)
    assert forced["ok"] is True, forced.get("output")
    assert not target.exists()
