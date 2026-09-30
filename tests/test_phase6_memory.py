"""Fase 6 - Brain link + memoria (criterio BLUEPRINT §13).

Criterio: lee el brain configurado (NEXUS_BRAIN si está seteada,
si no ./brain del proyecto); fallback local.
Cubre: brain link (lectura + fallback local), hierarchical_memory,
rag_engine (embeddings via Ollama local), multi_signal_retrieval.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from src.brain.link import brain_path

_BRAIN_DIR = brain_path()
_CEREBRO = _BRAIN_DIR / "cerebro.db"
_BRAIN_READY = _CEREBRO.exists() and _CEREBRO.stat().st_size > 1_000_000
_ENV_BRAIN = os.environ.get("NEXUS_BRAIN", "")


# --------------------------------------------------------------------------
# Brain link: lee cerebro.db del brain configurado
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _BRAIN_READY, reason="cerebro.db no disponible en este entorno")
def test_query_cerebro_reads_brain():
    from src.brain.link import brain_path, query_cerebro

    if _ENV_BRAIN:
        assert brain_path().resolve() == Path(_ENV_BRAIN).expanduser().resolve()
    else:
        assert brain_path().resolve() == (Path(__file__).resolve().parents[1] / "brain").resolve()
    tables = query_cerebro("SELECT count(*) FROM sqlite_master WHERE type='table'")
    assert tables[0][0] >= 10
    first = query_cerebro("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
    name = first[0][0]
    rows = query_cerebro(f"SELECT count(*) FROM [{name}]")
    assert isinstance(rows[0][0], int)


@pytest.mark.skipif(not _BRAIN_READY, reason="cerebro.db no disponible en este entorno")
def test_brain_info_readable():
    from src.brain.link import get_brain_info

    info = get_brain_info()
    assert info["exists"] is True
    assert info["files"]["cerebro.db"]["exists"] is True
    assert info["files"]["cerebro.db"]["bytes"] > 1_000_000
    # el flag refleja exactamente la heuristica de is_linked_to_v2()
    from src.brain.link import is_linked_to_v2

    assert info["linked_to_v2"] is is_linked_to_v2()


@pytest.mark.skipif(not _BRAIN_READY, reason="cerebro.db no disponible en este entorno")
def test_brain_health_readable():
    from src.brain.link import check_brain_health

    health = check_brain_health()
    assert health["cerebro.db"]["readable"] is True
    assert health["nexus_memory.db"]["readable"] is True


# --------------------------------------------------------------------------
# Fallback local: sin NEXUS_BRAIN -> ./brain del proyecto
# --------------------------------------------------------------------------

def test_fallback_local_brain(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_BRAIN", raising=False)
    from src.brain.link import DEFAULT_BRAIN, brain_exists, brain_path

    bp = brain_path()
    assert bp == DEFAULT_BRAIN
    assert bp == Path(__file__).resolve().parents[1] / "brain"
    assert bp.exists()
    assert brain_exists() is True


def test_fallback_local_brain_has_graph_db(monkeypatch):
    monkeypatch.delenv("NEXUS_BRAIN", raising=False)
    from src.brain.link import knowledge_graph_db

    kg = knowledge_graph_db()
    assert kg.exists() and kg.stat().st_size > 0


# --------------------------------------------------------------------------
# HierarchicalMemory (3 tiers, decay, triage, capacidad)
# --------------------------------------------------------------------------

@pytest.fixture
def hmem(monkeypatch, tmp_path):
    import src.core.hierarchical_memory as hm

    monkeypatch.setattr(hm, "_MEMORY_STORAGE", tmp_path / "hmem.json")
    return hm


def test_hierarchical_store_search_stats(hmem):
    m = hmem.HierarchicalMemory()
    item = m.store(
        "El router usa thompson sampling para seleccionar el mejor modelo provider",
        tags=["router"], source="test", importance=0.9, bypass_triage=True,
    )
    assert item is not None and item.id.startswith("mem_")
    hits = m.search("thompson")
    assert len(hits) == 1
    assert hits[0].access_count >= 1  # search incrementa acceso
    stats = m.get_stats()
    assert stats["total"] == 1
    assert "working" in stats["tiers"]


def test_hierarchical_triage_rejects_trivial(hmem):
    m = hmem.HierarchicalMemory()
    assert m.store("ok", bypass_triage=False) is None
    assert m.store("gracias", bypass_triage=False) is None
    assert m.get_stats()["total"] == 0


def test_hierarchical_capacity_enforcement(hmem):
    m = hmem.HierarchicalMemory(working_capacity=2)
    for i in range(3):
        m.store(f"contenido numero {i} sobre arquitectura del pipeline", bypass_triage=True)
    stats = m.get_stats()
    assert stats["total"] == 3
    assert stats["tiers"].get("working", {}).get("count") == 2
    assert stats["tiers"].get("episodic", {}).get("count") == 1


def test_hierarchical_consolidate_and_promote(hmem):
    m = hmem.HierarchicalMemory()
    item = m.store("patron de circuit breaker en el pipeline de providers", bypass_triage=True)
    assert item is not None
    # simula item viejo (>2h) con pocos accesos
    item.created_at -= 3 * 3600
    m.consolidate()
    assert m._items[0].tier == "episodic"
    stats = m.get_stats()
    assert stats["total"] == 1


# --------------------------------------------------------------------------
# RAG engine (Ollama nomic-embed-text) — criterio: embeddings locales
# --------------------------------------------------------------------------

def _ollama_up() -> bool:
    import httpx

    from src.core.environment import ollama_base_url

    try:
        r = httpx.get(f"{ollama_base_url()}/api/tags", timeout=3.0)
        return r.status_code == 200 and any(
            "embed" in m.get("name", "") or "nomic" in m.get("name", "")
            for m in r.json().get("models", [])
        )
    except Exception:
        return False


@pytest.mark.skipif(not _ollama_up(), reason="Ollama + modelo de embeddings no disponible")
def test_rag_engine_index_and_search(tmp_path):
    from src.core.rag_engine import RAGEngine

    async def run():
        db = str(tmp_path / "rag_test.db")
        doc = tmp_path / "doc.md"
        doc.write_text(
            "# Motor RAG\n"
            "El motor de retrieval augmented generation indexa documentos en chunks "
            "y calcula embeddings con nomic-embed-text a traves de Ollama local.\n"
            "La busqueda usa cosine similarity sobre los embeddings almacenados.\n",
            encoding="utf-8",
        )
        engine = RAGEngine(db_path=db)
        await engine.initialize()
        try:
            res = await engine.index_file(str(doc))
            assert res["status"] == "indexed"
            assert res["chunks"] >= 1
            assert res["errors"] == 0

            hits = await engine.search("embeddings cosine similarity Ollama", top_k=3)
            assert len(hits) >= 1
            assert hits[0]["score"] >= 0.3
            assert "cosine" in hits[0]["content"].lower() or "embedding" in hits[0]["content"].lower()

            st = await engine.stats()
            from src.core.environment import is_embed

            assert st["chunks"] >= 1 and is_embed(st["embed_model"])
        finally:
            await engine.close()

    asyncio.run(run())


# --------------------------------------------------------------------------
# MultiSignalRetrieval (vector + keyword + entity fusion)
# --------------------------------------------------------------------------

def test_multi_signal_fusion():
    from src.core.multi_signal_retrieval import MultiSignalRetrieval

    doc = "SuperNEXUS usa Thompson Sampling para elegir el mejor modelo provider"

    async def run():
        async def vec(q, top_k=10):
            return [{"content": doc, "score": 0.9, "source": "vec"}]

        ms = MultiSignalRetrieval(
            vector_search_fn=vec,
            keyword_search_fn=lambda q, top_k=10: [{"content": doc, "score": 0.7, "source": "kw"}],
            entity_extractor=MultiSignalRetrieval.extract_entities,
        )
        results = await ms.search("Thompson Sampling", top_k=5)
        assert len(results) == 1
        r = results[0]
        assert "vector" in r.signals and "keyword" in r.signals
        assert r.score >= 0.9
        assert r.score <= 1.15  # fusion + entity boost con tope implicito

    asyncio.run(run())


def test_multi_signal_entity_boost():
    from src.core.multi_signal_retrieval import MultiSignalRetrieval

    doc_a = "la receta de cocina con hierbas frescas"
    doc_b = "Thompson Sampling regula la exploracion de modelos"

    async def run():
        ms = MultiSignalRetrieval(
            vector_search_fn=None,
            keyword_search_fn=lambda q, top_k=10: [
                {"content": doc_a, "score": 0.5},
                {"content": doc_b, "score": 0.5},
            ],
            entity_extractor=lambda q: ["Thompson"],
        )
        results = await ms.search("Thompson", top_k=5)
        by_content = {r.content: r for r in results}
        assert "entity" in by_content[doc_b].signals
        assert by_content[doc_b].score > by_content[doc_a].score

    asyncio.run(run())


def test_multi_signal_resilient_to_failing_signals():
    from src.core.multi_signal_retrieval import MultiSignalRetrieval

    async def bad_vector(q, top_k=10):
        raise RuntimeError("vector backend caido")

    async def run():
        ms = MultiSignalRetrieval(
            vector_search_fn=bad_vector,
            keyword_search_fn=lambda q, top_k=10: [{"content": "fallback keyword", "score": 0.7}],
        )
        results = await ms.search("fallback", top_k=5)
        assert len(results) == 1
        assert results[0].signals == ["keyword"]

    asyncio.run(run())
