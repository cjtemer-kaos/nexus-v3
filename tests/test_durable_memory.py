import sqlite3

import pytest

import src.core.embeddings as embeddings
from src.core.knowledge_memory import KnowledgeMemory
from src.core.memory_context import _semantic_rerank, build_memory_context
from src.core.session_log import Message, SessionLog


def test_compact_summary_is_durable_and_reloads(tmp_path):
    db = tmp_path / "session.db"
    slog = SessionLog(db)
    slog.create_session(session_id="s1")
    slog.append("s1", Message(role="user", content="hola"))
    mid = slog.compact_mark("s1", "resumen durable")
    assert isinstance(mid, int)

    sess = slog.get_session("s1")
    assert sess.summary == "resumen durable"
    assert sess.summary_message_id == mid
    msg = slog.get_summary_message("s1")
    assert msg and msg["role"] == "system" and "resumen durable" in msg["content"]

    slog.append("s1", Message(role="user", content="despues"))
    assert [m["content"] for m in slog.get_messages_from("s1", mid)] == ["despues"]

    # "reinicio": nueva instancia sobre el mismo archivo
    slog2 = SessionLog(db)
    assert slog2.get_session("s1").summary_message_id == mid
    assert "resumen durable" in slog2.get_summary_message("s1")["content"]


def test_learn_creates_derived_from_and_informed_by(tmp_path):
    km = KnowledgeMemory(tmp_path / "kg.db", rag_index=False)
    src = km.learn("conocimiento fuente", topic_key="test/src", source="test",
                   persist_markdown=False, persist_observation=False)
    dst = km.learn("conocimiento nuevo", topic_key="test/dst", source="test",
                   persist_markdown=False, persist_observation=False,
                   session_id="sess-1", informed_by=[src["node_id"]])
    assert dst["typed_edges"]
    con = km._connect()
    try:
        rows = con.execute(
            "SELECT to_node, relation, note FROM edges WHERE from_node=?",
            (dst["node_id"],),
        ).fetchall()
    finally:
        con.close()
    pairs = {(int(r["to_node"]), r["relation"]) for r in rows}
    assert (int(src["node_id"]), "informed_by") in pairs
    session_node = km.ensure_session_node("sess-1")
    assert (int(session_node), "derived_from") in pairs


class _FakeMemory:
    def __init__(self):
        self.touched = []

    def search_terms(self, terms, limit=8, per_term=24):
        return [
            {"id": 1, "title": "alpha", "snippet": "alpha", "category": "x",
             "matches": 2, "score": 0.9},
            {"id": 2, "title": "beta", "snippet": "beta", "category": "x",
             "matches": 2, "score": 0.8},
        ]

    def touch_many(self, ids):
        self.touched = list(ids)


def _cfg(rerank="auto"):
    return {"memory": {"inject": {
        "enabled": True, "min_query_chars": 3, "min_terms": 1, "min_matches": 1,
        "limit": 2, "rerank": rerank, "rerank_timeout_ms": 1000,
    }}}


def test_semantic_rerank_reorders(monkeypatch):
    def fake_embed(texts, model="x", timeout=60):
        return [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]  # beta == query

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    mem = _FakeMemory()
    collect = {}
    out = build_memory_context("beta beta", memory=mem, config=_cfg(), collect=collect)
    assert collect["rerank"] == "semantic"
    assert out.index("[2]") < out.index("[1]")
    assert mem.touched == [2, 1]


def test_semantic_rerank_falls_back_without_embeddings(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sin embeddings")

    monkeypatch.setattr(embeddings, "embed", boom)
    mem = _FakeMemory()
    collect = {}
    out = build_memory_context("beta beta", memory=mem, config=_cfg(), collect=collect)
    assert collect["rerank"] == "fallback"
    assert out.index("[1]") < out.index("[2]")


def test_semantic_rerank_off_keeps_lexical(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no debe llamar embeddings")

    monkeypatch.setattr(embeddings, "embed", boom)
    hits = [{"id": 1, "score": 0.9, "title": "a", "snippet": "a"},
            {"id": 2, "score": 0.1, "title": "b", "snippet": "b"}]
    out, status, ms = _semantic_rerank("q", hits, {"rerank": "off"})
    assert status == "off" and out == hits
