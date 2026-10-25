"""Ledger de memoria: recall por turno, resultado del turno y veredictos."""
from __future__ import annotations

import sqlite3

import pytest

from src.core.memory_audit import VERDICT_WEIGHTS, MemoryAudit


@pytest.fixture()
def audit(tmp_path):
    return MemoryAudit(tmp_path / "audit.db")


def test_recall_y_outcome_quedan_atados(audit):
    guardados = audit.record_recall(
        session_id="s1", batch="s1:1", node_ids=[1, 2, 2], terms=["gate"], rerank="semantic",
        injected_chars=120,
    )
    assert guardados == 2
    assert audit.record_outcome(session_id="s1", batch="s1:1", status="ok") is True

    rep = audit.report(days=30)
    assert rep["totals"]["turns_with_memory"] == 1
    assert rep["totals"]["outcomes"] == 1
    assert rep["totals"]["with_outcome"] == 1
    assert rep["outcomes_by_status"] == {"ok": 1}
    assert {r["node_id"] for r in rep["top_recalled"]} == {1, 2}


def test_recall_sin_nodos_no_escribe(audit):
    assert audit.record_recall(session_id="s1", batch="s1:1", node_ids=[]) == 0
    assert audit.record_recall(session_id="s1", batch="", node_ids=[3]) == 0
    assert audit.record_recall(session_id="s1", batch="s1:2", node_ids=["x", -1]) == 0
    assert audit.report()["totals"]["recalls"] == 0


def test_outcome_sin_batch_cierra_el_ultimo_abierto(audit):
    audit.record_recall(session_id="s1", batch="s1:1", node_ids=[1])
    audit.record_recall(session_id="s2", batch="s2:1", node_ids=[2])
    assert audit.record_outcome(session_id="s1", status="error") is True

    con = sqlite3.connect(str(audit.db_path))
    try:
        filas = dict(con.execute("SELECT batch, status FROM outcomes").fetchall())
    finally:
        con.close()
    assert filas == {"s1:1": "error"}


def test_outcome_sin_recall_abierto_no_inventa_fila(audit):
    assert audit.record_outcome(session_id="fantasma", status="ok") is False


def test_standing_castiga_y_premia(audit):
    audit.add_verdict(1, "helpful")
    audit.add_verdict(2, "harmful")
    audit.add_verdict(3, "wrong")
    audit.add_verdict(4, "outdated")
    st = audit.standing_map()

    assert st[1] > 1.0
    assert st[2] < 1.0
    assert st[3] < st[2], "una nota falsa pesa mas que una danina"
    assert st[4] < 1.0
    assert audit.standing_map([1, 2]) == {1: st[1], 2: st[2]}
    assert set(VERDICT_WEIGHTS) == {"helpful", "outdated", "harmful", "wrong"}


def test_standing_sin_veredictos_es_neutro(audit):
    assert audit.standing_map([10, 11]) == {}


def test_veredicto_invalido_falla(audit):
    with pytest.raises(ValueError):
        audit.add_verdict(1, "me-gusta")
    with pytest.raises(ValueError):
        audit.add_verdict("x", "helpful")


def test_report_marca_castigados_y_mide_fallos(audit):
    audit.record_recall(session_id="s", batch="s:1", node_ids=[7, 8])
    audit.record_outcome(session_id="s", batch="s:1", status="error")
    audit.add_verdict(7, "wrong")

    rep = audit.report()
    por_id = {r["node_id"]: r for r in rep["top_recalled"]}
    assert por_id[7]["fail_rate"] == 1.0
    assert por_id[7]["turns_with_outcome"] == 1
    assert any(p["node_id"] == 7 for p in rep["penalized"])
    assert rep["min_standing"] == 0.6


def test_prune_respeta_los_veredictos(audit):
    audit.record_recall(session_id="s", batch="s:1", node_ids=[5])
    audit.record_outcome(session_id="s", batch="s:1", status="ok")
    con = sqlite3.connect(str(audit.db_path))
    try:
        con.execute("UPDATE recalls SET created_at='2020-01-01T00:00:00Z'")
        con.execute("UPDATE outcomes SET created_at='2020-01-01T00:00:00Z'")
        con.commit()
    finally:
        con.close()
    audit.add_verdict(5, "helpful")

    assert audit.prune(days=180) == 2
    assert audit.report()["totals"]["recalls"] == 0
    assert audit.standing_map([5])[5] > 1.0


# --------------------------------------------------------------------------
# Integracion: el veredicto cambia lo que viaja al prompt
# --------------------------------------------------------------------------
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
            "rerank": "off",
        }
    }
}


@pytest.fixture()
def km(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path))
    monkeypatch.setenv("NEXUS_V3_MEMORY_AUDIT", str(tmp_path / "memory_audit.db"))
    from src.core.hybrid_memory import HybridMemoryBackend
    from src.core.knowledge_memory import KnowledgeMemory, reset_knowledge_memory

    monkeypatch.setattr(HybridMemoryBackend, "_get_embedding", lambda self, text: [])
    reset_knowledge_memory()
    k = KnowledgeMemory(db_path=tmp_path / "knowledge_graph.db", rag_index=False)
    yield k
    reset_knowledge_memory()


def _learn(km, content: str, title: str) -> int:
    out = km.learn(
        content,
        title=title,
        category="lesson",
        source="test",
        index_rag=False,
        persist_markdown=False,
        persist_observation=False,
    )
    return int(out["node_id"])


def test_memoria_con_veredicto_malo_no_viaja(km, tmp_path):
    from src.core.memory_context import build_memory_context

    mala = _learn(km, "Approval gate en dispatch del registry con confirmacion.", "Gate viejo")
    buena = _learn(km, "Approval gate en dispatch del registry con auditoria.", "Gate nuevo")
    MemoryAudit(tmp_path / "memory_audit.db").add_verdict(mala, "wrong")

    block = build_memory_context(
        "approval gate dispatch registry", memory=km, config=CFG, session_id="s1",
        batch="s1:1",
    )
    assert block
    assert f"[{mala}]" not in block, "una memoria refutada no debe viajar"
    assert f"[{buena}]" in block


def test_recall_queda_registrado_con_su_batch(km, tmp_path):
    from src.core.memory_context import build_memory_context

    _learn(km, "Approval gate en dispatch del registry con auditoria.", "Gate")
    block = build_memory_context(
        "approval gate dispatch registry", memory=km, config=CFG, session_id="s1",
        batch="s1:7",
    )
    assert block

    con = sqlite3.connect(str(tmp_path / "memory_audit.db"))
    try:
        filas = con.execute("SELECT batch, session_id FROM recalls").fetchall()
    finally:
        con.close()
    assert filas == [("s1:7", "s1")]

