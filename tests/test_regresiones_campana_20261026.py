"""Regresiones de la campana de pruebas del 2026-10-26.

Cubre los defectos encontrados y corregidos:
- usage en cero en /v1/chat/completions (el split prompt/completion no salia del loop)
- /api/graph bloqueando el event loop (movido a to_thread)
- rag.search O(n) en Python puro (ahora matriz numpy cacheada)
- /api/graph/search y la tool recall devolviendo 0 con consultas multi-palabra
  (fts_search une los tokens con AND; ahora hay fallback a search_terms)
- /api/graph tardando 3.5 s por un len(set(...)) recalculado por nodo (O(n^2))
"""
from __future__ import annotations

import numpy as np

from src.core import rag
from src.core.agent_loop import LoopResult


def test_loop_result_expone_split_de_tokens():
    r = LoopResult(
        task="t", success=True, final_output="ok", steps=[], iterations=1,
        total_tokens=100, prompt_tokens=80, completion_tokens=20,
    )
    d = r.to_dict()
    assert d["total_tokens"] == 100
    assert d["prompt_tokens"] == 80
    assert d["completion_tokens"] == 20


def test_loop_result_split_por_defecto_en_cero():
    r = LoopResult(task="t", success=True, final_output="ok", steps=[], iterations=1)
    assert (r.prompt_tokens, r.completion_tokens) == (0, 0)
    assert r.to_dict()["prompt_tokens"] == 0


def test_score_numpy_ordena_y_filtra(monkeypatch):
    mat = np.array(
        [
            [1.0, 0.0],
            [0.0, 1.0],
            [0.7071, 0.7071],
        ],
        dtype=np.float32,
    )
    index = (mat, ["a", "b", "c"], ["contenido a", "contenido b", "contenido c"])
    res = rag._score_numpy(index, [1.0, 0.0], min_score=0.5)
    assert [src for _, src, _ in res][0] == "a"
    assert all(s >= 0.5 for s, _, _ in res)
    # 'b' es ortogonal (score 0) -> queda fuera
    assert "b" not in [src for _, src, _ in res]
    scores = [s for s, _, _ in res]
    assert scores == sorted(scores, reverse=True)


def test_cached_index_invalida_por_conteo(tmp_path, monkeypatch):
    """La cache se rearma cuando cambia (count, max(id)) del indice."""
    import sqlite3

    db_path = tmp_path / "rag.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, "
        "chunk_index INTEGER, content TEXT, content_hash TEXT, embedding BLOB, created_at REAL)"
    )
    con.execute(
        "INSERT INTO chunks (source, chunk_index, content, content_hash, embedding, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("s1", 0, "uno", "h1", rag._vec_to_blob([1.0, 0.0]), 0.0),
    )
    con.commit()

    idx1 = rag._cached_index(con)
    assert idx1 is not None
    assert idx1[1] == ["s1"]

    con.execute(
        "INSERT INTO chunks (source, chunk_index, content, content_hash, embedding, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("s2", 0, "dos", "h2", rag._vec_to_blob([0.0, 1.0]), 0.0),
    )
    con.commit()

    idx2 = rag._cached_index(con)
    assert idx2 is not None
    assert idx2[1] == ["s1", "s2"], "la cache debe invalidarse al entrar chunks nuevos"
    con.close()


def test_blob_roundtrip():
    v = [0.5, -1.25, 3.0]
    assert rag._blob_to_vec(rag._vec_to_blob(v)) == v


class _FakeKM:
    """Grafo falso: reproduce el AND de fts_search y la fusion por termino."""

    def __init__(self, estricto: dict[str, list[dict]] | None = None,
                 por_termino: list[dict] | None = None):
        self.estricto = estricto or {}
        self.por_termino = por_termino or []
        self.llamadas: list[tuple] = []

    def fts_search(self, query: str, limit: int = 20):
        self.llamadas.append(("fts", query, limit))
        return self.estricto.get(query, [])

    def search_terms(self, terms, limit: int = 10, per_term: int = 24):
        self.llamadas.append(("terms", list(terms), limit))
        return self.por_termino


def _nodo(nid=14324):
    return {"id": nid, "title": "contenido uno de la prueba de dedupe",
            "category": "general", "topic_key": "barrido/prueba-dedupe",
            "revision": 1, "snippet": "contenido uno", "rank": -1.0}


def test_fts_tolerante_cae_a_search_terms_con_varias_palabras():
    """'barrido prueba dedupe' no matchea con AND: el fallback debe encontrar el nodo."""
    from src.api.server import _fts_search_tolerant

    km = _FakeKM(estricto={}, por_termino=[_nodo()])
    res = _fts_search_tolerant(km, "barrido prueba dedupe", limit=20)

    assert [r["id"] for r in res] == [14324]
    assert km.llamadas[0][0] == "fts", "primero intenta la busqueda estricta"
    assert km.llamadas[1][0] == "terms"
    assert "barrido" in km.llamadas[1][1] and "dedupe" in km.llamadas[1][1]


def test_fts_tolerante_respeta_hits_estrictos():
    """Si la busqueda estricta encuentra algo, no se degrada a la fusion."""
    from src.api.server import _fts_search_tolerant

    km = _FakeKM(estricto={"dedupe": [_nodo()]}, por_termino=[_nodo(999)])
    res = _fts_search_tolerant(km, "dedupe", limit=20)

    assert [r["id"] for r in res] == [14324]
    assert [c[0] for c in km.llamadas] == ["fts"]


def test_fts_tolerante_una_sola_palabra_sin_hits_no_fusiona():
    """Una sola palabra que no existe no debe inventar resultados."""
    from src.api.server import _fts_search_tolerant

    km = _FakeKM(estricto={}, por_termino=[_nodo()])
    assert _fts_search_tolerant(km, "inexistentexyz", limit=20) == []
    assert [c[0] for c in km.llamadas] == ["fts"]


def test_fts_tolerante_query_vacia():
    from src.api.server import _fts_search_tolerant

    km = _FakeKM(por_termino=[_nodo()])
    assert _fts_search_tolerant(km, "   ") == []
    assert km.llamadas == []


def test_fts_tolerante_no_propaga_excepciones():
    """Si el grafo falla, la API debe devolver vacio y no un 500."""
    from src.api.server import _fts_search_tolerant

    class _Roto:
        def fts_search(self, *a, **k):
            raise RuntimeError("db bloqueada")

        def search_terms(self, *a, **k):
            raise RuntimeError("db bloqueada")

    assert _fts_search_tolerant(_Roto(), "barrido prueba dedupe") == []


def test_get_messages_de_sesion_inexistente_da_404():
    """Antes devolvia 200 con historial vacio: el cliente creia que era una sesion nueva."""
    from fastapi.testclient import TestClient

    from src.api.server import app

    r = TestClient(app).get("/api/sessions/no-existe-abc123/messages")
    assert r.status_code == 404, r.text
    assert "inexistente" in r.json()["detail"].lower()


def test_post_mensaje_a_sesion_inexistente_da_404_y_no_500():
    """Antes el INSERT chocaba con la FK y salia un 500 por un error del cliente."""
    from fastapi.testclient import TestClient

    from src.api.server import app

    r = TestClient(app).post(
        "/api/sessions/no-existe-abc123/messages",
        json={"role": "user", "content": "huerfano?"},
    )
    assert r.status_code == 404, r.text


def _grafo_sintetico(dir_brain, nodos, aristas):
    """Crea un knowledge_graph.db minimo con el esquema que lee _load_graph_data."""
    import sqlite3

    dir_brain.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(dir_brain / "knowledge_graph.db"))
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, content TEXT, title TEXT,"
        " category TEXT, source TEXT, importance INTEGER, tags TEXT,"
        " content_hash TEXT, created_at TEXT, updated_at TEXT, access_count INTEGER,"
        " last_accessed TEXT, topic_key TEXT, revision_count INTEGER)"
    )
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, from_node INTEGER,"
        " to_node INTEGER, relation TEXT, weight REAL, note TEXT, created_at TEXT)"
    )
    for nid, titulo, cat in nodos:
        con.execute(
            "INSERT INTO nodes (id, content, title, category, source, importance, tags,"
            " content_hash, created_at, updated_at, access_count, topic_key, revision_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, f"contenido {nid}", titulo, cat, "test", 5, "[]", f"h{nid}",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 0, "", 1),
        )
    for u, v in aristas:
        con.execute(
            "INSERT INTO edges (from_node, to_node, relation, weight, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (u, v, "relates_to", 0.5, "", "2026-01-01T00:00:00Z"),
        )
    con.commit()
    con.close()


def test_graph_data_reporta_las_comunidades_reales(tmp_path, monkeypatch):
    """stats['communities'] debe coincidir con las comunidades que traen los nodos.

    El conteo se calculaba dentro del loop de nodos (len(set(...)) por nodo):
    O(n^2) y 3.3 s de CPU con 12k nodos. Ahora se calcula una sola vez.
    """
    from src.api.server import _load_graph_data

    brain = tmp_path / "brain"
    _grafo_sintetico(
        brain,
        [(1, "uno", "concept"), (2, "dos", "concept"), (3, "tres", "fix"), (4, "cuatro", "fix")],
        [(1, 2), (3, 4)],
    )
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(brain))

    data = _load_graph_data(limit=100)
    assert len(data["nodes"]) == 4, "los 4 nodos tienen aristas, no son aislados"
    comunidades = {n["community"] for n in data["nodes"]}
    assert data["stats"]["communities"] == len(comunidades)
    assert all(n["color"].startswith("#") and len(n["color"]) == 7 for n in data["nodes"])


def test_graph_data_comunidad_unica_usa_color_por_categoria(tmp_path, monkeypatch):
    """Con una sola comunidad el color sale de la categoria, no de la paleta."""
    from src.api.server import _load_graph_data

    brain = tmp_path / "brain"
    _grafo_sintetico(brain, [(1, "uno", "fix"), (2, "dos", "fix")], [(1, 2)])
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(brain))

    data = _load_graph_data(limit=100)
    assert data["stats"]["communities"] == 1
    # "fix" -> #3DDC84 en CATEGORY_COLOR (local a _load_graph_data)
    assert all(n["color"] == "#3DDC84" for n in data["nodes"])


def test_mensajes_de_sesion_real_sigue_funcionando():
    """La sesion existe -> 200 (no rompimos el camino normal)."""
    from fastapi.testclient import TestClient

    from src.api.server import _session_log, app

    sess = _session_log().create_session(project="test-regresion-404")
    try:
        c = TestClient(app)
        assert c.get(f"/api/sessions/{sess.id}/messages").status_code == 200
        r = c.post(
            f"/api/sessions/{sess.id}/messages",
            json={"role": "user", "content": "hola"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["session_id"] == sess.id
    finally:
        _session_log().delete_session(sess.id)
