"""Deteccion de updates por manifiesto: hashes, diff y validacion segura."""
from __future__ import annotations

import json

import pytest

from src.core.updater import (
    build_manifest, compare, load_manifest, validate_manifest,
)


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "node_modules").mkdir()
    (root / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "data" / "session.db").write_bytes(b"no debe hashearse")
    (root / "node_modules" / "x.js").write_text("no\n", encoding="utf-8")
    return root


def test_build_manifest_excludes_data_and_modules(project):
    m = build_manifest(project, version="1.2.3", commit="abc")
    validate_manifest(m)
    paths = [f["path"] for f in m["files"]]
    assert paths == ["README.md", "src/a.py"]
    assert m["version"] == "1.2.3"
    assert m["commit"] == "abc"
    assert m["counts"]["files"] == 2
    assert m["knowledge"]["bundle_schema"] == "nexus.knowledge.bundle/v1"


def test_load_local_manifest(project, tmp_path):
    m = build_manifest(project)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(m), encoding="utf-8")
    assert load_manifest(path)["content_hash"] == m["content_hash"]


def test_compare_detects_changes_and_missing(project):
    m = build_manifest(project)
    assert compare(m, project)["update_available"] is False

    (project / "src" / "a.py").write_text("print('b')\n", encoding="utf-8")
    res = compare(m, project)
    assert res["update_available"] is True
    assert res["counts"] == {"total": 2, "changed": 1, "missing": 0, "unchanged": 1}
    assert res["changed"][0]["path"] == "src/a.py"
    assert res["auto_apply"] is False

    (project / "README.md").unlink()
    res = compare(m, project)
    assert res["counts"]["missing"] == 1


def test_manifest_rejects_traversal_and_bad_hash(project):
    m = build_manifest(project)
    bad = json.loads(json.dumps(m))
    bad["files"][0]["path"] = "../evil.py"
    bad["content_hash"] = ""
    with pytest.raises(ValueError, match="fuera del proyecto|invalida"):
        validate_manifest(bad)

    bad2 = json.loads(json.dumps(m))
    bad2["files"][0]["sha256"] = "zz"
    with pytest.raises(ValueError, match="sha256"):
        validate_manifest(bad2)


def test_tampered_manifest_is_rejected(project):
    m = build_manifest(project)
    m["files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="content_hash"):
        validate_manifest(m)


def test_unknown_schema_is_rejected(project):
    m = build_manifest(project)
    m["schema"] = "otro/v1"
    with pytest.raises(ValueError, match="schema"):
        validate_manifest(m)
