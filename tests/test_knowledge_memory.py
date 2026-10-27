"""Fase grafo A — KnowledgeMemory (Método OPCODE para nexus-v3).

Cubre: migración idempotente (topic_key/revision/FTS5/entities), UPSERT por
topic_key, dedupe por hash 24h, auto-conexión shared_entity con tope
12 aristas/nodo, búsqueda FTS5, find_path, maintain (dedupe+selfloop+tope),
y el endpoint POST /api/learn/direct.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.core.knowledge_memory import (
    MAX_EDGES_PER_NODE,
    KnowledgeMemory,
    reset_knowledge_memory,
)


@pytest.fixture()
def km(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path))
    from src.core.hybrid_memory import HybridMemoryBackend
    monkeypatch.setattr(HybridMemoryBackend, "_get_embedding", lambda self, text: [])
    reset_knowledge_memory()
    k = KnowledgeMemory(db_path=tmp_path / "knowledge_graph.db", rag_index=False)
    yield k
    reset_knowledge_memory()


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

def test_schema_idempotent(km, tmp_path):
    # segunda instancia re-ejecuta ensure_schema sin error
    k2 = KnowledgeMemory(db_path=tmp_path / "knowledge_graph.db", rag_index=False)
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "knowledge_graph.db"))
    cols = {r[1] for r in con.execute("PRAGMA table_info(nodes)")}
    assert {"topic_key", "revision_count"} <= cols
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert "nodes_fts" in names
    assert "entities" in names
    assert "entity_nodes" in names
    con.close()
    assert k2.status()["fts"] is True


def test_migrates_existing_graph_and_backfills_entities(tmp_path):
    import sqlite3
    db = tmp_path / "legacy-knowledge-graph.db"
    con = sqlite3.connect(str(db))
    con.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL DEFAULT 'nexus', importance INTEGER NOT NULL DEFAULT 5,
            tags TEXT NOT NULL DEFAULT '[]', content_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0, last_accessed TEXT
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_node INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            to_node INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            relation TEXT NOT NULL DEFAULT 'relates_to',
            weight REAL NOT NULL DEFAULT 1.0, note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(from_node, to_node, relation)
        );
        INSERT INTO nodes (content,title,category,source,importance,tags,content_hash,created_at,updated_at)
        VALUES ('The old graph mentions "AtlasTokenizer".', 'Legacy AtlasTokenizer', 'general', 'migration', 5, '[]', 'legacyhash', '2026-01-01', '2026-01-01');
        INSERT INTO nodes (content,title,category,source,importance,tags,content_hash,created_at,updated_at)
        VALUES ('A second old note mentions "AtlasTokenizer".', 'Second Legacy AtlasTokenizer', 'general', 'migration', 5, '[]', 'legacyhash2', '2026-01-02', '2026-01-02');
    """)
    con.commit()
    con.close()

    km = KnowledgeMemory(db_path=db, rag_index=False)
    assert km.fts_search("AtlasTokenizer")
    backfill = km.maintain()
    assert backfill["entity_edges_added"] >= 1
    assert km.maintain()["entity_edges_added"] == 0
    r = km.learn('New information about "AtlasTokenizer".', topic_key="legacy/atlas")
    related = km.find_related(1)
    assert r["edges_added"] >= 1
    assert any(item["other_id"] == r["node_id"] for item in related["related"])


def test_status_counts(km):
    km.learn("nexus guarda conocimiento en el grafo FTS5", topic_key="grafo/fts")
    st = km.status()
    assert st["nodes"] == 1
    assert st["topic_nodes"] == 1
    assert st["fts"] is True
    assert st["edges_headroom"] == st["max_edges"] - st["edges"]
    assert st["max_edges"] == 60000


def test_entrypoint_keeps_v3_brain_self_contained(monkeypatch):
    """v3 must not inherit another project's ambient NEXUS_V3_BRAIN."""
    monkeypatch.setenv("NEXUS_V3_BRAIN", r"D:\otro\proyecto\brain")
    monkeypatch.delenv("NEXUS_V3_BRAIN", raising=False)
    from src.api.server import _ensure_local_brain, _PROJECT_ROOT
    resolved = _ensure_local_brain()
    assert resolved == str(_PROJECT_ROOT / "brain")
    assert os.environ["NEXUS_V3_BRAIN"] == str(_PROJECT_ROOT / "brain")


def test_entrypoint_honours_v3_scoped_override(tmp_path, monkeypatch):
    from src.api.server import _ensure_local_brain
    # register NEXUS_V3_BRAIN so monkeypatch restores it after direct writes
    monkeypatch.setenv("NEXUS_V3_BRAIN", os.environ.get("NEXUS_V3_BRAIN", ""))
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path))
    assert _ensure_local_brain() == str(tmp_path)
    assert os.environ["NEXUS_V3_BRAIN"] == str(tmp_path)



def test_maintenance_never_prunes_unique_nodes(km):
    for i in range(24):
        km.learn(
            f"Durable unique record number {i}.",
            topic_key=f"infinite-memory/record-{i}",
            persist_observation=False,
        )
    before = km.status()["nodes"]
    km.maintain()
    assert before == 24
    assert km.status()["nodes"] == before


def test_rag_uses_same_local_brain_root(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path))
    from src.core.rag import rag_db_path
    assert rag_db_path() == tmp_path / "rag_index.db"


# --------------------------------------------------------------------------
# learn: upsert + dedupe
# --------------------------------------------------------------------------

def test_learn_upsert_topic_key(km):
    r1 = km.learn("El grafo usa FTS5 para recall", topic_key="grafo/recall")
    assert r1["ok"] and not r1["deduped"]
    assert r1["revision"] == 1
    r2 = km.learn("El grafo usa FTS5 bm25 para recall", topic_key="grafo/recall")
    assert r2["node_id"] == r1["node_id"]
    assert r2["revision"] == 2
    st = km.status()
    assert st["nodes"] == 1
    assert st["total_revisions"] == 1
    import sqlite3
    con = sqlite3.connect(str(km.db_path.parent / "hybrid_memory.db"))
    assert con.execute(
        "SELECT count(*) FROM memories WHERE topic_key=? AND category=?",
        ("grafo/recall", "general"),
    ).fetchone()[0] == 1
    con.close()


def test_learn_identico_no_sube_revision(km):
    """Reaprender el MISMO contenido con topic_key no debe crear una revision falsa.

    Antes: revision 2, una copia archivada en node_revisions y re-indexado de RAG
    aunque el contenido fuera byte a byte igual.
    """
    r1 = km.learn("El router persiste en data/router_state.json", topic_key="router/estado")
    assert r1["deduped"] is False and r1["revision"] == 1

    r2 = km.learn("El router persiste en data/router_state.json", topic_key="router/estado")
    assert r2["node_id"] == r1["node_id"]
    assert r2["deduped"] is True
    assert r2.get("outputs", {}).get("graph") == "deduped"

    st = km.status()
    assert st["nodes"] == 1
    assert st["total_revisions"] == 0, "no debe archivar una copia identica"

    import sqlite3
    con = sqlite3.connect(str(km.db_path))
    row = con.execute(
        "SELECT revision_count, importance FROM nodes WHERE topic_key='router/estado'"
    ).fetchone()
    con.close()
    assert row[0] == 1, "revision_count no debe incrementarse sin cambios"

    # ... pero reafirmar SI puede subir la importancia (recencia/prioridad)
    r3 = km.learn("El router persiste en data/router_state.json",
                  topic_key="router/estado", importance=9)
    assert r3["deduped"] is True
    con = sqlite3.connect(str(km.db_path))
    imp = con.execute(
        "SELECT importance FROM nodes WHERE topic_key='router/estado'"
    ).fetchone()[0]
    con.close()
    assert imp == 9


def test_learn_contenido_distinto_si_sube_revision(km):
    """El camino de supersede sigue intacto cuando el contenido cambia."""
    r1 = km.learn("version uno", topic_key="cambia/contenido")
    r2 = km.learn("version dos", topic_key="cambia/contenido")
    assert r2["deduped"] is False
    assert r2["revision"] == 2
    assert km.status()["total_revisions"] == 1


def test_learn_dedupe_hash_24h(km):
    r1 = km.learn("hecho repetido sin topic key")
    r2 = km.learn("hecho repetido sin topic key")
    assert r1["deduped"] is False
    assert r2["deduped"] is True
    assert r2["node_id"] == r1["node_id"]
    assert km.status()["nodes"] == 1


def test_concurrent_topic_upsert_stays_single_node(km):
    from concurrent.futures import ThreadPoolExecutor

    def write(i):
        return km.learn(
            f"revision {i} of concurrent durable knowledge",
            topic_key="concurrency/shared-topic",
            persist_observation=False,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write, range(6)))
    assert len({r["node_id"] for r in results}) == 1
    assert km.status()["nodes"] == 1
    assert km.fts_search("revision")[0]["revision"] == 6


def test_learn_outputs_reported(km):
    r = km.learn("contenido de prueba con salida markdown", topic_key="test/outputs")
    outs = r["outputs"]
    assert outs["graph"] == "ok"
    assert outs["markdown"] == "ok"
    # rag deshabilitado en fixture; observation usa el store híbrido local
    assert outs["rag"].startswith("skip")
    assert outs["observation"] == "ok"
    # markdown real
    md = Path(os.environ["NEXUS_V3_BRAIN"]) / "learnings.md"
    assert md.exists()
    assert "test/outputs" in md.read_text(encoding="utf-8")
    import sqlite3
    con = sqlite3.connect(str(Path(os.environ["NEXUS_V3_BRAIN"]) / "hybrid_memory.db"))
    assert con.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
    con.close()


# --------------------------------------------------------------------------
# entities + auto-conexión
# --------------------------------------------------------------------------

def test_entities_shared_edge(km):
    r1 = km.learn('Guardamos "AtlasTokenizer" en memoria', topic_key="ent/one")
    r2 = km.learn('Usamos "AtlasTokenizer" de nuevo', topic_key="ent/two")
    assert any(e.lower() == "atlastokenizer" for e in r1["entities"])
    assert r2["edges_added"] >= 1
    rel = km.find_related(r1["node_id"])
    assert any(x["relation"] == "shared_entity" for x in rel["related"])
    ents = km.get_entities()
    assert any(e["name"].lower() == "atlastokenizer" and e["nodes"] >= 2 for e in ents)


def test_edge_cap_per_node(km):
    # 15 nodos que comparten la misma entidad -> ningún nodo supera el tope
    for i in range(15):
        km.learn(f"versión {i} de 'HyperRelay' en el sistema", topic_key=f"cap/{i}")
    import sqlite3
    con = sqlite3.connect(str(km.db_path))
    rows = con.execute(
        "SELECT node_id, count(*) FROM ("
        "SELECT from_node AS node_id FROM edges UNION ALL "
        "SELECT to_node FROM edges) GROUP BY node_id"
    ).fetchall()
    con.close()
    assert rows, "debe haber aristas auto-conectadas"
    assert max(r[1] for r in rows) <= MAX_EDGES_PER_NODE


# --------------------------------------------------------------------------
# consultas
# --------------------------------------------------------------------------

def test_fts_search(km):
    km.learn("ZephyrCache reduce la latencia del grafo", topic_key="perf/zephyr")
    hits = km.fts_search("ZephyrCache")
    assert hits and hits[0]["topic_key"] == "perf/zephyr"
    assert "Zephyr" in hits[0]["snippet"] or "ZephyrCache" in hits[0]["title"]


def test_find_path(km):
    a = km.learn("nodo alpha", topic_key="path/a")["node_id"]
    b = km.learn("nodo beta", topic_key="path/b")["node_id"]
    c = km.learn("nodo gamma", topic_key="path/c")["node_id"]
    import sqlite3
    con = sqlite3.connect(str(km.db_path))
    now = "2026-01-01T00:00:00Z"
    con.execute("INSERT INTO edges (from_node,to_node,relation,weight,note,created_at) VALUES (?,?,?,?,?,?)",
                (a, b, "relates_to", 1.0, "", now))
    con.execute("INSERT INTO edges (from_node,to_node,relation,weight,note,created_at) VALUES (?,?,?,?,?,?)",
                (b, c, "relates_to", 1.0, "", now))
    con.commit()
    con.close()
    res = km.find_path(a, c, max_depth=4)
    assert res["found"] is True
    assert res["path"] == [a, b, c]


def test_get_central(km):
    a = km.learn("central uno", topic_key="cen/a")["node_id"]
    for i in range(3):
        other = km.learn(f"vecino {i}", topic_key=f"cen/n{i}")["node_id"]
        import sqlite3
        con = sqlite3.connect(str(km.db_path))
        con.execute("INSERT INTO edges (from_node,to_node,relation,weight,note,created_at) VALUES (?,?,?,?,?,?)",
                    (a, other, "relates_to", 1.0, "", "2026-01-01T00:00:00Z"))
        con.commit()
        con.close()
    top = km.get_central(5)
    assert top and top[0]["id"] == a
    assert top[0]["degree"] == 3


# --------------------------------------------------------------------------
# maintain
# --------------------------------------------------------------------------

def test_maintain_dedupe_and_selfloop(km):
    import sqlite3
    r1 = km.learn("hecho duplicado para dedupe")  # sin topic_key
    # fuerza un duplicado directo (mismo hash, sin topic) + self-loop
    con = sqlite3.connect(str(km.db_path))
    now = "2026-01-01T00:00:00Z"
    row = con.execute("SELECT content_hash FROM nodes WHERE id=?", (r1["node_id"],)).fetchone()
    con.execute(
        "INSERT INTO nodes (content,title,category,source,importance,tags,content_hash,"
        "created_at,updated_at,access_count,topic_key,revision_count) VALUES (?,?,?,?,?,?,?,?,?,0,'',1)",
        ("hecho duplicado para dedupe", "dup", "general", "test", 5, "[]",
         row[0], now, now),
    )
    dup_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute("INSERT INTO edges (from_node,to_node,relation,weight,note,created_at) VALUES (?,?,?,?,?,?)",
                (dup_id, dup_id, "relates_to", 1.0, "", now))
    con.commit()
    con.close()
    res = km.maintain()
    assert res["self_loops"] >= 1
    assert res["deduped_nodes"] == 1
    st = km.status()
    import sqlite3 as s
    con = s.connect(str(km.db_path))
    loops = con.execute("SELECT count(*) FROM edges WHERE from_node=to_node").fetchone()[0]
    # el grupo con mismo hash quedó en 1 sola fila
    n_same = con.execute("SELECT count(*) FROM nodes WHERE content_hash=?", (row[0],)).fetchone()[0]
    con.close()
    assert loops == 0
    assert n_same == 1
    assert st["nodes"] == 1


def test_maintain_edge_cap(km):
    import sqlite3
    a = km.learn("nodo base", topic_key="tope/a")["node_id"]
    con = sqlite3.connect(str(km.db_path))
    now = "2026-01-01T00:00:00Z"
    for i in range(20):
        con.execute(
            "INSERT INTO nodes (content,title,category,source,importance,tags,content_hash,"
            "created_at,updated_at,access_count,topic_key,revision_count) VALUES (?,?,?,?,?,?,?,?,?,0,?,1)",
            (f"filler {i}", f"f{i}", "general", "test", 5, "[]", f"h{i}", now, now, f"fill/{i}"),
        )
        nid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute("INSERT INTO edges (from_node,to_node,relation,weight,note,created_at) VALUES (?,?,?,?,?,?)",
                    (a, nid, "relates_to", 1.0, "", now))
    con.commit()
    con.close()
    st = km.status()
    assert st["edges"] == 20
    # enforce tope usando el max bajo (monkeypatch temporal)
    import src.core.knowledge_memory as kmm
    old = kmm.MAX_EDGES
    try:
        kmm.MAX_EDGES = 10
        res = km.maintain()
        assert res["edges_pruned"] == 10
        assert km.status()["edges"] == 10
    finally:
        kmm.MAX_EDGES = old


# --------------------------------------------------------------------------
# endpoint /api/learn/direct
# --------------------------------------------------------------------------

def test_endpoint_learn_direct(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path))
    from src.core.hybrid_memory import HybridMemoryBackend
    monkeypatch.setattr(HybridMemoryBackend, "_get_embedding", lambda self, text: [])
    monkeypatch.setattr(KnowledgeMemory, "_rag_index", lambda self, node_id, title, content: (True, "stub"))
    reset_knowledge_memory()
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from src.api.server import app

    client = TestClient(app)
    r = client.post("/api/learn/direct", json={
        "content": "Nexus v3 aprende por learn/direct con topic_key estable",
        "topic": "opcode/learn-direct",
        "category": "architecture",
        "tags": ["opcode", "grafo"],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["topic_key"] == "opcode/learn-direct"
    assert data["outputs"]["graph"] == "ok"

    # upsert: segunda llamada incrementa revision
    r2 = client.post("/api/learn/direct", json={
        "content": "Nexus v3 aprende por learn/direct (v2)",
        "topic": "opcode/learn-direct",
    })
    assert r2.status_code == 200
    assert r2.json()["revision"] == 2
    assert r2.json()["node_id"] == data["node_id"]

    # status + search + entities + related
    st = client.get("/api/graph/status")
    assert st.status_code == 200
    assert st.json()["nodes"] == 1
    assert st.json()["fts"] is True

    se = client.get("/api/graph/search", params={"q": "learn/direct"})
    assert se.status_code == 200
    assert se.json()["count"] >= 1

    en = client.get("/api/graph/entities")
    assert en.status_code == 200

    rel = client.get(f"/api/graph/related/{data['node_id']}")
    assert rel.status_code == 200
    assert rel.json()["node"]["id"] == data["node_id"]

    # API de grafo compatible + filtros por relación/peso + centralidad/path.
    first = client.post("/api/learn/direct", json={
        "content": 'Primera nota sobre "GraphFilterToken".',
        "topic": "graph-filter/first",
    }).json()
    second = client.post("/api/learn/direct", json={
        "content": 'Segunda nota sobre "GraphFilterToken".',
        "topic": "graph-filter/second",
    }).json()
    assert second["edges_added"] >= 1

    graph_response = client.get("/api/knowledge/graph", params={
        "search": "GraphFilterToken", "relation": "shared_entity",
        "min_weight": 0.5, "centrality": "degree", "include_isolated": "true",
    })
    assert graph_response.status_code == 200
    graph_data = graph_response.json()
    assert graph_data["stats"]["centrality"] == "degree"
    assert graph_data["stats"]["filtered_edges"] >= 1
    assert graph_data["stats"]["total_edges"] >= graph_data["stats"]["filtered_edges"]

    weight_filtered = client.get("/api/graph", params={
        "relation": "shared_entity", "min_weight": 0.6,
        "include_isolated": "true",
    })
    assert weight_filtered.json()["stats"]["filtered_edges"] == 0
    path = client.get(
        f"/api/graph/path/{first['node_id']}/{second['node_id']}"
    )
    assert path.status_code == 200
    assert path.json()["found"] is True
    assert path.json()["path"] == [first["node_id"], second["node_id"]]

    # content requerido
    bad = client.post("/api/learn/direct", json={"content": ""})
    assert bad.status_code == 400
    reset_knowledge_memory()


# --------------------------------------------------------------------------
# historial de revisiones (supersede-not-delete)
# --------------------------------------------------------------------------

def test_upsert_archiva_la_revision_saliente(km):
    r1 = km.learn("version uno del nodo", topic_key="hist/nodo")
    r2 = km.learn("version dos del nodo", topic_key="hist/nodo")
    assert r1["node_id"] == r2["node_id"]
    h = km.node_history(r1["node_id"])
    assert h["current"]["revision"] == 2
    assert h["current"]["content"] == "version dos del nodo"
    assert h["revision_count"] == 1
    assert h["revisions"][0]["revision"] == 1
    assert h["revisions"][0]["content"] == "version uno del nodo"


def test_upsert_archiva_cada_revision_en_orden(km):
    r = km.learn("v1", topic_key="hist/cadena", persist_observation=False)
    for i in (2, 3, 4):
        r = km.learn(f"v{i}", topic_key="hist/cadena", persist_observation=False)
    h = km.node_history(r["node_id"])
    assert h["current"]["revision"] == 4
    assert [x["revision"] for x in h["revisions"]] == [3, 2, 1]
    assert [x["content"] for x in h["revisions"]] == ["v3", "v2", "v1"]
    assert km.status()["archived_revisions"] == 3


def test_restore_es_append_only_y_reversible(km):
    km.learn("contenido A", topic_key="hist/restore", persist_observation=False)
    b = km.learn("contenido B", topic_key="hist/restore", persist_observation=False)
    nid = b["node_id"]

    res = km.restore_revision(nid, 1)
    assert res["ok"] and res["restored_from"] == 1 and res["new_revision"] == 3
    h = km.node_history(nid)
    assert h["current"]["content"] == "contenido A"  # volvio la version vieja
    assert h["current"]["revision"] == 3
    # B no se perdio: quedo archivada
    assert any(x["revision"] == 2 and x["content"] == "contenido B" for x in h["revisions"])

    # y el restore es reversible: puedo volver a B
    assert km.restore_revision(nid, 2)["new_revision"] == 4
    assert km.node_history(nid)["current"]["content"] == "contenido B"


def test_restore_refresca_fts(km):
    r1 = km.learn('usamos "RestoreToken" en la version vieja', topic_key="hist/fts",
                  persist_observation=False)
    km.learn("contenido nuevo sin la entidad", topic_key="hist/fts",
             persist_observation=False)
    # al pisarla, la version vieja deja de estar indexada (solo se indexa la actual)
    assert not km.fts_search("RestoreToken")

    h = km.node_history(r1["node_id"])
    vieja = h["revisions"][-1]["revision"]
    assert km.restore_revision(r1["node_id"], vieja)["ok"]

    hits = km.fts_search("RestoreToken")
    assert hits and hits[0]["id"] == r1["node_id"], "tras restaurar, FTS5 la vuelve a hallar"


def test_restore_revision_inexistente(km):
    r = km.learn("nodo sin historial", topic_key="hist/sin", persist_observation=False)
    res = km.restore_revision(r["node_id"], 99)
    assert not res["ok"] and "no encontrada" in res["error"]
    assert km.restore_revision(999999, 1)["ok"] is False


def test_restore_requiere_topic_key(km):
    r = km.learn("nodo dedupe sin topic_key", persist_observation=False)
    res = km.restore_revision(r["node_id"], 1)
    assert not res["ok"] and "topic_key" in res["error"]


def test_history_de_nodo_inexistente(km):
    assert "error" in km.node_history(424242)


def test_endpoint_graph_history_and_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path))
    from src.core.hybrid_memory import HybridMemoryBackend
    monkeypatch.setattr(HybridMemoryBackend, "_get_embedding", lambda self, text: [])
    reset_knowledge_memory()
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from src.api.server import app

    client = TestClient(app)
    n1 = client.post("/api/learn/direct", json={
        "content": "primera version del nodo", "topic": "api/hist",
    }).json()
    n2 = client.post("/api/learn/direct", json={
        "content": "segunda version del nodo", "topic": "api/hist",
    }).json()
    assert n2["revision"] == 2 and n2["node_id"] == n1["node_id"]

    h = client.get(f"/api/graph/node/{n1['node_id']}/history")
    assert h.status_code == 200, h.text
    data = h.json()
    assert data["current"]["revision"] == 2
    assert data["revisions"][0]["content"] == "primera version del nodo"

    vacio = client.post(f"/api/graph/node/{n1['node_id']}/restore", json={})
    assert vacio.status_code == 400
    r = client.post(f"/api/graph/node/{n1['node_id']}/restore", json={"revision": 1})
    assert r.status_code == 200, r.text
    assert r.json()["new_revision"] == 3
    assert client.get(f"/api/graph/node/{n1['node_id']}/history").json()["current"]["content"] == "primera version del nodo"

    assert client.post(f"/api/graph/node/{n1['node_id']}/restore", json={"revision": 77}).status_code == 404
    assert client.get("/api/graph/node/999999/history").status_code == 404
    reset_knowledge_memory()
