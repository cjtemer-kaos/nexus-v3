"""Replay con `seq` + epoch + hueco explícito.

El defecto que fijan: `replay` devolvía eventos desde un timestamp y nada más.
Un cliente reconectado no podía distinguir "no hubo eventos nuevos" de "el log
se podó y perdí eventos", así que se quedaba con un historial incompleto sin
saberlo.
"""
from __future__ import annotations

import pytest

from src.core.event_store import EventStore


@pytest.fixture()
def store(tmp_path):
    es = EventStore(tmp_path / "events.db")
    yield es
    es.close()


def _tres(store, sid="s1"):
    for i in range(3):
        store.append_event("tool_result", {"i": i}, session_id=sid)


def test_seq_es_monotonico_por_sesion(store):
    _tres(store, "s1")
    _tres(store, "s2")
    s1 = store.get_events(session_id="s1")
    s2 = store.get_events(session_id="s2")
    assert [e.seq for e in s1] == [1, 2, 3]
    assert [e.seq for e in s2] == [1, 2, 3], "cada sesión numera desde 1"
    assert all(e.replay_epoch == store.epoch() for e in s1)


def test_replay_desde_ultima_seq_no_repite(store):
    _tres(store)
    visto = store.replay_window("s1")
    assert len(visto["events"]) == 3
    assert visto["latest_seq"] == 3 and visto["gap"] is False

    nuevo = store.replay_window("s1", since_seq=visto["latest_seq"])
    assert nuevo["events"] == []
    assert nuevo["latest_seq"] == 3
    assert nuevo["gap"] is False, "sin novedades no hay hueco"
    assert nuevo["replayed"] is True

    store.append_event("tool_result", {"i": 3}, session_id="s1")
    cola = store.replay_window("s1", since_seq=3)
    assert [e["seq"] for e in cola["events"]] == [4]
    assert cola["latest_seq"] == 4


def test_epoch_distinto_es_gap(store):
    _tres(store)
    epoch = store.epoch()
    store.bump_epoch()

    w = store.replay_window("s1", since_seq=3, client_epoch=epoch)
    assert w["gap"] is True, "el cliente quedó con un epoch viejo"
    assert w["replay_epoch"] == epoch + 1

    # Con el epoch al día no hay gap.
    ok = store.replay_window("s1", since_seq=3, client_epoch=epoch + 1)
    assert ok["gap"] is False


def test_seq_vieja_inexistente_es_gap(store):
    _tres(store)
    # Simula una poda de las primeras filas: el cliente pide desde 0 pero lo
    # más viejo que queda es seq=3.
    store._get_conn().execute("DELETE FROM events WHERE seq < 3")
    w = store.replay_window("s1", since_seq=1)
    assert [e["seq"] for e in w["events"]] == [3]
    assert w["gap"] is True, "faltan eventos entre 1 y 3 y hay que re-sincronizar"


def test_latest_seq_por_sesion(store):
    _tres(store, "a")
    store.append_event("tool_result", {"i": 0}, session_id="b")
    assert store.latest_seq("a") == 3
    assert store.latest_seq("b") == 1
    assert store.latest_seq("no-existe") == 0
