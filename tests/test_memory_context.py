"""Memoria automatica del grafo: recuperacion, presupuesto y telemetria."""
from __future__ import annotations

import sqlite3

import pytest

from src.core.knowledge_memory import KnowledgeMemory, reset_knowledge_memory
from src.core.memory_context import build_memory_context, memory_stats, query_terms


class FakeEvents:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def append_event(self, kind, data=None, **kwargs):
        self.events.append((kind, data or {}, kwargs))
        return "evt-1"


CFG = {
    "memory": {
        "inject": {
            "enabled": True,
            "limit": 3,
            "max_terms": 10,
            "min_terms": 2,
            "min_matches": 2,
            "max_chars": 2400,
            "snippet_chars": 320,
        }
    }
}


@pytest.fixture()
def km(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path))
    from src.core.hybrid_memory import HybridMemoryBackend

    monkeypatch.setattr(HybridMemoryBackend, "_get_embedding", lambda self, text: [])
    reset_knowledge_memory()
    k = KnowledgeMemory(db_path=tmp_path / "knowledge_graph.db", rag_index=False)
    yield k
    reset_knowledge_memory()


def _learn(km, content: str, title: str, category: str = "lesson", topic_key: str = "") -> int:
    out = km.learn(
        content,
        title=title,
        category=category,
        topic_key=topic_key,
        source="test",
        index_rag=False,
        persist_markdown=False,
        persist_observation=False,
    )
    return int(out["node_id"])


def test_query_terms_drops_code_urls_and_stopwords():
    terms = query_terms(
        "¿Cómo funciona el approval gate de nexus-v3? "
        "```python\nprint('x')\n``` https://ejemplo.com/ruta"
    )
    assert "approval" in terms
    assert "gate" in terms
    assert "nexus" in terms
    assert "como" not in terms
    assert "https" not in terms
    assert "print" not in terms
    assert len(terms) == len(set(terms))


def test_injects_relevant_node_and_records_usage(km):
    gate_id = _learn(
        km,
        "El approval gate se cablea en ToolRegistry.dispatch antes de ejecutar la tool.",
        "Approval gate en ToolRegistry.dispatch",
    )
    _learn(
        km,
        "El grafo guarda nodos con topic_key y aristas tipadas supersedes.",
        "Grafo, topic_key y aristas",
        category="concept",
    )
    events = FakeEvents()
    block = build_memory_context(
        "¿dónde se cablea el approval gate y por qué no está activo?",
        memory=km,
        event_store=events,
        config=CFG,
        session_id="ses-1",
    )
    assert block.startswith("MEMORIA DEL GRAFO")
    assert f"[{gate_id}]" in block
    assert "ToolRegistry.dispatch" in block
    assert events.events, "debe emitir memory_used"
    kind, data, kwargs = events.events[0]
    assert kind == "memory_used"
    assert gate_id in data["node_ids"]
    assert kwargs.get("session_id") == "ses-1"
    con = sqlite3.connect(str(km.db_path))
    try:
        used = con.execute(
            "SELECT access_count FROM nodes WHERE id=?", (gate_id,)
        ).fetchone()[0]
    finally:
        con.close()
    assert used == 1


def test_no_hits_returns_empty(km):
    _learn(km, "El grafo guarda nodos y aristas.", "Grafo")
    assert build_memory_context(
        "kubernetes ingress controller con certificados",
        memory=km,
        config=CFG,
    ) == ""


def test_budget_limits_output(km):
    for i in range(5):
        _learn(km, f"approval gate detalle {i} " + "x" * 600, f"Gate detalle {i}")
    cfg = {"memory": {"inject": dict(CFG["memory"]["inject"], max_chars=400)}}
    block = build_memory_context("approval gate detalle", memory=km, config=cfg)
    assert block
    assert len(block) <= 400


def test_disabled_returns_empty(km):
    _learn(km, "approval gate en dispatch", "Approval gate")
    cfg = {"memory": {"inject": {"enabled": False}}}
    assert build_memory_context(
        "approval gate en dispatch", memory=km, config=cfg
    ) == ""


def test_min_matches_filters_single_term(km):
    weak = _learn(km, "Historia del gate de la ciudad.", "Gate historico")
    _learn(
        km,
        "Approval gate y dispatch de tools en Nexus v3.",
        "Approval gate nexus dispatch",
    )
    block = build_memory_context(
        "approval gate nexus dispatch tools",
        memory=km,
        config=CFG,
    )
    assert block
    assert f"[{weak}]" not in block


def test_search_terms_ranks_multi_match_first(km):
    _learn(km, "Approval como concepto general.", "Approval suelto")
    both = _learn(km, "Approval gate con dispatch.", "Approval gate dispatch")
    hits = km.search_terms(["approval", "gate"], limit=5)
    assert hits
    assert hits[0]["id"] == both
    assert hits[0]["matches"] == 2


def test_memory_stats_counts_used_nodes(km):
    node = _learn(km, "Approval gate medido.", "Approval gate medido")
    km.touch_many([node])
    stats = memory_stats(km)
    assert stats["nodes"] == 1
    assert stats["used_nodes"] == 1
    assert stats["uses"] >= 1
    st = km.status()
    assert st["used_nodes"] == 1
    assert st["uses"] >= 1
    assert st["last_used"]


def test_search_terms_dedupes_identical_content(km):
    a = _learn(km, "approval gate con contenido duplicado exacto", "Duplicado A")
    con = sqlite3.connect(str(km.db_path))
    try:
        con.execute("UPDATE nodes SET content_hash='hash-historico-distinto' WHERE id=?", (a,))
        con.commit()
    finally:
        con.close()
    b = _learn(km, "approval gate con contenido duplicado exacto", "Duplicado B")
    assert b != a
    hits = km.search_terms(["approval", "gate"], limit=5)
    same = [h for h in hits if h["id"] in (a, b)]
    assert len(same) == 1
