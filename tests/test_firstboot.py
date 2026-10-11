"""Tests de primer arranque — RAG + aprendizaje, todo con stubs (sin Ollama/red)."""
from __future__ import annotations

from pathlib import Path

from src.core.firstboot import ensure_learning, ensure_rag_ready, firstboot_report


def _vec(dim=8):
    return [[float(i + 1) for i in range(dim)]]


class _FakeKM:
    def __init__(self):
        self.maintain_calls = 0

    def status(self):
        return {"nodes": 3, "edges": 1, "fts": True}

    def learn(self, content, topic_key=""):
        return {"ok": True}

    def maintain(self):
        self.maintain_calls += 1
        return {"ok": True, "deduped_nodes": 0}


# ---------------------------------------------------------------- RAG

def test_rag_ready_con_stubs(tmp_path):
    rep = ensure_rag_ready(models=["nomic-embed-text:latest", "qwen2.5:7b"],
                           embed_fn=lambda texts: _vec(), db_path=tmp_path / "rag.db")
    assert rep["ready"] is True
    assert rep["embed_available"] is True
    assert rep["index_ok"] is True
    assert rep["smoke_ok"] is True
    assert (tmp_path / "rag.db").exists()


def test_rag_reusa_indice_existente(tmp_path):
    db = tmp_path / "rag.db"
    ensure_rag_ready(models=["nomic-embed-text"], embed_fn=lambda t: _vec(), db_path=db)
    rep2 = ensure_rag_ready(models=["nomic-embed-text"], embed_fn=lambda t: _vec(), db_path=db)
    assert rep2["ready"] is True and rep2["index_ok"] is True


def test_rag_sin_ollama_no_lanza(tmp_path):
    rep = ensure_rag_ready(models=[], embed_fn=lambda t: _vec(), db_path=tmp_path / "r.db")
    assert rep["index_ok"] is True
    assert rep["ready"] is True  # sin deteccion se respeta la config
    assert any("sin_respuesta" in a or "sin deteccion" in a for a in rep["actions"])


def test_rag_embed_vacio_no_ready(tmp_path):
    rep = ensure_rag_ready(models=["nomic-embed-text"], embed_fn=lambda t: [[]],
                           db_path=tmp_path / "r.db")
    assert rep["smoke_ok"] is False
    assert rep["ready"] is False
    assert rep["errors"]


def test_rag_embed_roto_no_lanza(tmp_path):
    def boom(texts):
        raise RuntimeError("ollama caido")

    rep = ensure_rag_ready(models=["nomic-embed-text"], embed_fn=boom, db_path=tmp_path / "r.db")
    assert rep["ready"] is False
    assert any("smoke" in e for e in rep["errors"])


# ---------------------------------------------------------------- learning

def test_learning_con_fake_factory(tmp_path):
    fakes = []

    def factory(p: Path):
        f = _FakeKM()
        fakes.append(f)
        return f

    rep = ensure_learning(brain_dir=tmp_path / "brain", knowledge_factory=factory,
                          run_maintenance=True)
    assert rep["ready"] is True
    assert rep["graph_ok"] is True
    assert rep["maintenance_wired"] is True
    assert fakes and fakes[0].maintain_calls == 1
    assert rep["gates"].get("learning_service_import") is True
    assert rep["gates"].get("skillopt_heldout_gate") is True


def test_learning_sin_maintenance_no_muta(tmp_path):
    fakes = []

    def factory(p: Path):
        f = _FakeKM()
        fakes.append(f)
        return f

    rep = ensure_learning(brain_dir=tmp_path / "b", knowledge_factory=factory,
                          run_maintenance=False)
    assert rep["ready"] is True
    assert fakes[0].maintain_calls == 0


def test_learning_maintain_cada_6h():
    from src.core.learning_service import _GRAPH_MAINTENANCE_INTERVAL_S
    assert _GRAPH_MAINTENANCE_INTERVAL_S == 21600.0


def test_learning_factory_rota_no_lanza(tmp_path):
    def factory(p: Path):
        raise RuntimeError("db bloqueada")

    rep = ensure_learning(brain_dir=tmp_path / "b", knowledge_factory=factory,
                          check_gates=False)
    assert rep["ready"] is False
    assert rep["errors"]


def test_firstboot_report_agregado(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path / "brain"))
    manifest = {"version": 1, "entries": []}
    import src.core.mcp_setup as ms
    monkeypatch.setattr(ms, "load_manifest", lambda p=None: manifest)
    rep = firstboot_report(models=["nomic-embed-text"], embed_fn=lambda t: _vec(),
                           brain_dir=tmp_path / "brain")
    # mcp vacio -> ready; rag/learn dependen de stubs
    assert set(rep) == {"ready", "mcp", "rag", "learning"}
    assert rep["rag"]["ready"] is True
