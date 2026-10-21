"""Bundles portables del grafo: export, dry-run, merge, dedupe y revisiones."""
from __future__ import annotations

import copy

import pytest

from src.core import knowledge_bundle as kb
from src.core.knowledge_bundle import (
    export_bundle, import_bundle, read_bundle, validate_bundle, write_bundle,
)
from src.core.knowledge_memory import KnowledgeMemory


@pytest.fixture()
def graphs(tmp_path):
    src = KnowledgeMemory(tmp_path / "src.db", rag_index=False)
    dst = KnowledgeMemory(tmp_path / "dst.db", rag_index=False)
    a = src.learn("El backend usa FastAPI.", topic_key="arch:backend",
                  category="architecture", title="Backend", tags=["api"],
                  source="test", importance=8)
    b = src.learn("La UI usa React.", topic_key="arch:ui",
                  category="architecture", title="UI", tags=["react"],
                  source="test", importance=7)
    src.learn("Nota suelta sobre pytest.", category="note", title="Tests",
              source="test")
    src.add_relation(a["node_id"], b["node_id"], "depends_on", 0.9, "ui consume api")
    return src, dst


def test_export_compact_and_validate(graphs, tmp_path):
    src, _ = graphs
    bundle = export_bundle(src)
    assert bundle["schema"] == kb.SCHEMA
    assert bundle["counts"] == {"nodes": 3, "edges": 1}
    assert bundle["content_hash"]
    validate_bundle(bundle)

    path = write_bundle(bundle, tmp_path / "cerebro.nexuskg.json")
    again = read_bundle(path)
    assert again["content_hash"] == bundle["content_hash"]
    node_keys = {n["key"] for n in again["nodes"]}
    assert "arch:backend" in node_keys
    assert again["edges"][0]["from"] == "arch:backend"


def test_import_dry_run_does_not_write(graphs):
    src, dst = graphs
    bundle = export_bundle(src)

    dry = import_bundle(dst, bundle, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["nodes"] == {"new": 3, "updated": 0, "skipped": 0, "total": 3}
    assert dry["edges"]["new"] == 1
    assert dst.status()["nodes"] == 0


def test_import_merge_is_idempotent(graphs):
    src, dst = graphs
    bundle = export_bundle(src)

    first = import_bundle(dst, bundle, dry_run=False)
    assert first["nodes"]["new"] == 3
    assert first["edges"]["new"] == 1

    second = import_bundle(dst, bundle, dry_run=False)
    assert second["nodes"] == {"new": 0, "updated": 0, "skipped": 3, "total": 3}
    assert second["edges"]["new"] == 0
    assert second["edges"]["skipped"] == 1
    assert dst.status()["nodes"] == 3


def test_newer_topic_key_updates_and_archives_revision(graphs):
    src, dst = graphs
    bundle = export_bundle(src)
    import_bundle(dst, bundle, dry_run=False)

    newer = copy.deepcopy(bundle)
    for n in newer["nodes"]:
        if n["topic_key"] == "arch:ui":
            n["content"] = "La UI usa React + Vite."
            n["updated_at"] = "2999-01-01T00:00:00Z"
    newer["content_hash"] = kb._canonical_hash(newer["nodes"], newer["edges"])

    res = import_bundle(dst, newer, dry_run=False)
    assert res["nodes"]["updated"] == 1
    assert res["nodes"]["skipped"] == 2

    con = dst._connect()
    try:
        node = con.execute(
            "SELECT revision_count, content FROM nodes WHERE topic_key='arch:ui'"
        ).fetchone()
        revisions = con.execute("SELECT COUNT(*) FROM node_revisions").fetchone()[0]
    finally:
        con.close()
    assert node["revision_count"] == 2
    assert "Vite" in node["content"]
    assert revisions == 1


def test_edited_bundle_is_rejected(graphs):
    src, _ = graphs
    bundle = export_bundle(src)
    bad = copy.deepcopy(bundle)
    bad["nodes"][0]["content"] = "adulterado"
    with pytest.raises(ValueError, match="content_hash"):
        validate_bundle(bad)


def test_unknown_schema_and_oversized_content(graphs):
    src, _ = graphs
    bundle = export_bundle(src)
    bad = copy.deepcopy(bundle)
    bad["schema"] = "nexus.knowledge.bundle/v0"
    with pytest.raises(ValueError, match="schema"):
        validate_bundle(bad)

    huge = copy.deepcopy(bundle)
    huge["nodes"][0]["content"] = "x" * (kb.MAX_CONTENT_CHARS + 1)
    huge["content_hash"] = kb._canonical_hash(huge["nodes"], huge["edges"])
    with pytest.raises(ValueError, match="excede"):
        validate_bundle(huge)
