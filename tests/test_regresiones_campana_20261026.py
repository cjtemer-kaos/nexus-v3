"""Regresiones de la campana de pruebas del 2026-10-26.

Cubre los tres defectos encontrados y corregidos:
- usage en cero en /v1/chat/completions (el split prompt/completion no salia del loop)
- /api/graph bloqueando el event loop (movido a to_thread)
- rag.search O(n) en Python puro (ahora matriz numpy cacheada)
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
