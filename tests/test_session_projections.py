"""S1-paso3: folds deterministas + cache por revision (proyecciones puras)."""
from __future__ import annotations

from src.core.session_log import Message, SessionLog
from src.core.session_projections import (
    ProjectionCache,
    fold_session_stats,
    fold_turn_outline,
    make_cache_key,
)
from src.core.session_store import SessionStore


def _log_db(tmp_path):
    return SessionStore(str(tmp_path / "session.db"))


def _msgs():
    return [
        Message(role="user", content="hola", tokens=2, created_at="2026-01-01T00:00:00+00:00"),
        Message(role="assistant", content="buenas", tokens=3, created_at="2026-01-01T00:00:10+00:00"),
        Message(role="tool", content="begin", tool_name="turn:start", created_at="2026-01-01T00:00:11+00:00"),
        Message(role="user", content="haz X con mucho detalle para el preview del turno uno", tokens=5, created_at="2026-01-01T00:00:12+00:00"),
        Message(role="tool", content="end", tool_name="turn:end", created_at="2026-01-01T00:00:13+00:00"),
    ]


def test_stats_fold_determinista():
    a = fold_session_stats(_msgs())
    b = fold_session_stats(_msgs())
    assert a == b
    assert a.total == 5
    assert a.counts == {"assistant": 1, "tool": 2, "user": 2}
    assert a.total_tokens == 10
    assert a.wall_seconds == 13.0
    assert a.first_at.startswith("2026-01-01T00:00:00")


def test_stats_vacio_wall_cero():
    s = fold_session_stats([])
    assert s.total == 0 and s.wall_seconds == 0.0 and s.counts == {}


def test_outline_turnos_y_preview():
    entries = fold_turn_outline(_msgs(), preview_chars=80)
    by_turn = {e.turn: e for e in entries}
    # Preambulo (turn 0) con los 2 primeros mensajes.
    assert by_turn[0].seq_start == 0 and by_turn[0].message_count == 2
    # Turno 1: start + user + end.
    assert by_turn[1].seq_start == 2 and by_turn[1].seq_end == 4
    assert by_turn[1].message_count == 3
    assert "haz X" in by_turn[1].preview
    # Determinista.
    assert fold_turn_outline(_msgs()) == fold_turn_outline(_msgs())


def test_outline_end_huerfano_no_pierde_mensajes():
    msgs = [
        Message(role="tool", content="end", tool_name="turn:end"),
        Message(role="user", content="solo"),
    ]
    entries = fold_turn_outline(msgs)
    assert sum(e.message_count for e in entries) == 2


def test_cache_hit_miss_por_revision(tmp_path):
    store = _log_db(tmp_path)
    try:
        h = store.create(project="p")
        sid = h.id
        h.append([Message(role="user", content="uno")])
        rev1 = store.stat(sid).revision
        live = h.read()
        cache = ProjectionCache()
        _, _, hit1 = cache.get_or_compute(sid, rev1, live)
        _, _, hit2 = cache.get_or_compute(sid, rev1, live)
        assert (hit1, hit2) == (False, True)
        assert make_cache_key(sid, rev1) == (sid, rev1)
        # Nueva escritura -> revision nueva -> miss + refresh.
        h.append([Message(role="user", content="dos")])
        rev2 = store.stat(sid).revision
        assert rev2 != rev1
        assert cache.needs_refresh(sid, rev1, rev2) is True
        assert cache.needs_refresh(sid, rev2, rev2) is False
        _, _, hit3 = cache.get_or_compute(sid, rev2, h.read())
        assert hit3 is False
        assert cache.invalidate(sid) >= 1
    finally:
        store.close()


def test_proyeccion_no_toca_tablas(tmp_path):
    store = _log_db(tmp_path)
    try:
        h = store.create()
        h.append(_msgs())
        before = store.stat(h.id).event_count
        cache = ProjectionCache()
        cache.get_or_compute(h.id, store.stat(h.id).revision, h.read())
        assert store.stat(h.id).event_count == before == 5
    finally:
        store.close()
