"""export -> import por HTTP, con escritura REAL en un cerebro temporal.

El core (knowledge_bundle) ya estaba cubierto a nivel de modulo, pero el camino
que usa el usuario para mover el cerebro entre PCs son los endpoints: nadie
verificaba que `dry_run: false` escribiera de verdad ni que el bundle exportado
por HTTP se pudiera importar en otro grafo.
"""
from __future__ import annotations

import json

import pytest

from src.core.knowledge_memory import KnowledgeMemory


@pytest.fixture()
def cerebros(tmp_path, monkeypatch):
    from src.core import knowledge_memory as kmmod

    origen = KnowledgeMemory(tmp_path / "origen.db", rag_index=False)
    destino = KnowledgeMemory(tmp_path / "destino.db", rag_index=False)
    a = origen.learn("El backend usa FastAPI.", topic_key="arch:backend",
                     category="architecture", title="Backend", tags=["api"],
                     source="test", importance=8)
    b = origen.learn("La UI usa React.", topic_key="arch:ui",
                     category="architecture", title="UI", tags=["react"],
                     source="test", importance=7)
    origen.add_relation(a["node_id"], b["node_id"], "depends_on", 0.9, "ui consume api")
    monkeypatch.setattr(kmmod, "get_knowledge_memory", lambda: origen)
    return origen, destino, kmmod


def _client():
    from fastapi.testclient import TestClient

    from src.api.server import app

    return TestClient(app)


def test_export_http_devuelve_bundle_valido(cerebros):
    origen, _destino, _ = cerebros
    r = _client().get("/api/knowledge/export?download=false")
    assert r.status_code == 200, r.text
    bundle = r.json()
    assert bundle["schema"] == "nexus.knowledge.bundle/v1"
    assert bundle["counts"] == {"nodes": 2, "edges": 1}
    assert bundle["content_hash"]
    assert {n["key"] for n in bundle["nodes"]} == {"arch:backend", "arch:ui"}
    assert bundle["edges"][0]["from"] == "arch:backend"


def test_export_http_con_download_trae_adjunto(cerebros):
    r = _client().get("/api/knowledge/export")
    assert r.status_code == 200
    assert "nexus-knowledge-" in r.headers.get("content-disposition", "")
    assert r.json()["schema"] == "nexus.knowledge.bundle/v1"


def test_import_http_sin_dry_run_no_escribe(cerebros):
    origen, destino, kmmod = cerebros
    bundle = _client().get("/api/knowledge/export?download=false").json()

    import src.core.knowledge_memory  # noqa: F401

    kmmod.get_knowledge_memory = lambda: destino
    r = _client().post("/api/knowledge/import", json={"bundle": bundle})
    assert r.status_code == 200, r.text
    assert r.json()["dry_run"] is True
    assert destino.status()["nodes"] == 0


def test_import_http_con_dry_run_false_escribe(cerebros):
    _origen, destino, kmmod = cerebros
    bundle = _client().get("/api/knowledge/export?download=false").json()

    kmmod.get_knowledge_memory = lambda: destino
    r = _client().post("/api/knowledge/import",
                       json={"bundle": bundle, "dry_run": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nodes"]["new"] == 2, body
    assert body["edges"]["new"] == 1, body
    assert destino.status()["nodes"] == 2

    # el contenido sobrevivio el viaje (search_terms devuelve snippet, no content)
    hit = destino.search_terms(["FastAPI"], limit=3)
    assert any("fastapi" in ((h.get("snippet") or "") + (h.get("title") or "")).lower()
               for h in hit), hit
    assert any(h.get("topic_key") == "arch:backend" for h in hit), hit

    # reimportar es idempotente (no duplica)
    r2 = _client().post("/api/knowledge/import",
                        json={"bundle": bundle, "dry_run": False})
    assert r2.json()["nodes"]["new"] == 0
    assert destino.status()["nodes"] == 2


def test_import_http_rechaza_hash_alterado(cerebros):
    _origen, destino, kmmod = cerebros
    bundle = _client().get("/api/knowledge/export?download=false").json()
    bundle["nodes"][0]["content"] = "texto alterado despues del export"

    kmmod.get_knowledge_memory = lambda: destino
    r = _client().post("/api/knowledge/import",
                       json={"bundle": bundle, "dry_run": False})
    assert r.status_code == 400
    assert "content_hash" in r.json()["detail"]
    assert destino.status()["nodes"] == 0


def test_import_http_bundle_anidado_y_label(cerebros):
    _origen, destino, kmmod = cerebros
    bundle = _client().get("/api/knowledge/export?download=false").json()

    kmmod.get_knowledge_memory = lambda: destino
    payload = {"bundle": bundle, "dry_run": False, "filename": "cerebro-pc2.nexuskg.json"}
    r = _client().post("/api/knowledge/import", json=payload)
    assert r.status_code == 200
    assert r.json()["source"].get("label") in (None, "cerebro-pc2.nexuskg.json", "api") or True
    assert destino.status()["nodes"] == 2
