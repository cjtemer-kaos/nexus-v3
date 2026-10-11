"""S1-paso3: session query — filtros / trazas / search + cursor (solo SELECTs)."""
from __future__ import annotations

import pytest

from src.core.session_log import Message, SessionLog
from src.core.session_query import (
    decode_cursor,
    encode_cursor,
    ensure_query_index,
    filter_sessions,
    fts_schema_fingerprint,
    get_event_trace,
    get_lineage,
    make_snippet,
    needs_refresh,
    search_sessions,
)


@pytest.fixture
def slog(tmp_path):
    s = SessionLog(str(tmp_path / "session.db"))
    yield s
    s.close()


def _seed(slog: SessionLog):
    a = slog.create_session(project="p1")
    slog.append(a.id, Message(role="user", content="refactor auth module cwd=/repo/alpha"))
    slog.append(a.id, Message(role="assistant", content="auth done"))
    slog.append(a.id, Message(role="user", content="otra cosa trivial"))
    b = slog.create_session(project="p1", parent_id=a.id)
    slog.append(b.id, Message(role="user", content="refactor billing module"))
    c = slog.create_session(project="other")
    slog.append(c.id, Message(role="user", content="refactor auth again"))
    return a, b, c


def test_filtros_id_parent_project(slog):
    a, b, _ = _seed(slog)
    assert [s.id for s in filter_sessions(slog, ids=[a.id, b.id])] != []
    got = filter_sessions(slog, ids=[a.id, b.id])
    assert {s.id for s in got} == {a.id, b.id}  # OR dentro de ids
    assert [s.id for s in filter_sessions(slog, parent_id=a.id)] == [b.id]
    assert {s.id for s in filter_sessions(slog, project="p1")} == {a.id, b.id}
    assert filter_sessions(slog, id_prefix=a.id[:6])
    assert filter_sessions(slog, parent_id="nadie") == []
    # AND entre dimensiones: parent + project que no coincide => vacio.
    assert filter_sessions(slog, parent_id=a.id, project="other") == []


def test_filtro_cwd_fallback_sin_ddl(slog):
    a, _, _ = _seed(slog)
    before = fts_schema_fingerprint(slog)
    got = filter_sessions(slog, cwd="/repo/alpha")
    assert [s.id for s in got] == [a.id]
    assert fts_schema_fingerprint(slog) == before


def test_lineage_ancestros_e_hijos(slog):
    a, b, _ = _seed(slog)
    lin = get_lineage(slog, b.id)
    assert lin.session and lin.session.id == b.id
    assert [s.id for s in lin.ancestors] == [a.id]
    lin_a = get_lineage(slog, a.id)
    assert [s.id for s in lin_a.children] == [b.id]
    assert get_lineage(slog, "no-existe").session is None


def test_event_trace_seq_paginado(slog):
    a, _, _ = _seed(slog)
    page = get_event_trace(slog, a.id, offset=1, limit=2)
    assert [e["seq"] for e in page] == [1, 2]
    assert page[0]["role"] == "assistant"
    with pytest.raises(ValueError):
        get_event_trace(slog, a.id, offset=-1)


def test_snippet_recorta_con_elipsis():
    content = "x" * 200 + " refactor auth " + "y" * 200
    snip = make_snippet(content, "refactor auth")
    assert "refactor auth" in snip and len(snip) < len(content)
    assert make_snippet("hola mundo", "") == "hola mundo"
    assert "…" in make_snippet(content, "refactor auth")


def test_search_snippet_y_paginacion_cursor(slog):
    a, b, c = _seed(slog)
    before = fts_schema_fingerprint(slog)
    p1 = search_sessions(slog, "refactor", limit=2)
    assert len(p1.hits) == 2 and p1.has_more and p1.next_cursor
    assert all("refactor" in h.snippet.lower() for h in p1.hits)
    assert decode_cursor(p1.next_cursor) == 2
    assert encode_cursor(0) != ""
    p2 = search_sessions(slog, "refactor", limit=2, cursor=p1.next_cursor)
    assert len(p2.hits) >= 1 and not p2.has_more
    # Sin solape entre paginas (mismo contenido+sesion no repetido).
    keys1 = {(h.session_id, h.snippet) for h in p1.hits}
    keys2 = {(h.session_id, h.snippet) for h in p2.hits}
    assert not (keys1 & keys2)
    assert {a.id, b.id, c.id} >= {h.session_id for h in (*p1.hits, *p2.hits)}
    # Por sesion.
    only_a = search_sessions(slog, "auth", session_id=a.id, limit=10)
    assert only_a.hits and all(h.session_id == a.id for h in only_a.hits)
    # Query vacia / cursor malo no rompen.
    assert search_sessions(slog, "   ").hits == ()
    assert search_sessions(slog, "refactor", cursor="!!!").hits
    # FTS intacto: sin ALTER ni rebuild.
    assert fts_schema_fingerprint(slog) == before


def test_cursor_invalido_empieza_en_cero():
    assert decode_cursor("") == 0
    assert decode_cursor("no-base64!!!") == 0


def test_ensure_aux_no_toca_fts_y_needs_refresh(slog):
    before = fts_schema_fingerprint(slog)
    ensure_query_index(slog)
    ensure_query_index(slog)  # idempotente
    assert fts_schema_fingerprint(slog) == before
    assert needs_refresh("1:t", "2:t") is True
    assert needs_refresh("1:t", "1:t") is False
