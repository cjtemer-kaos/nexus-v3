"""S1-fase1: session store con version + handle (sobre SessionLog, sin tocarlo)."""
from __future__ import annotations

import threading

import pytest

from src.core.session_log import Message
from src.core.session_store import (
    SESSION_FORMAT_VERSION,
    SessionAlreadyOwnedError,
    SessionFormatUnsupportedError,
    SessionHandleClosedError,
    SessionNotFoundError,
    SessionReadOnlyError,
    SessionStore,
)


@pytest.fixture
def store(tmp_path):
    s = SessionStore(str(tmp_path / "session.db"))
    yield s
    s.close()


def _msg(content: str = "hola", **kw) -> Message:
    return Message(role="user", content=content, **kw)


def test_header_roundtrip(store):
    h = store.create(project="nexus-v3")
    sid = h.id
    snap = store.stat(sid)
    assert snap.header.id == sid
    assert snap.header.version == SESSION_FORMAT_VERSION == 0
    assert snap.header.project == "nexus-v3"
    assert snap.event_count == 0
    h.close()


def test_version_mismatch_rejected(store):
    h = store.create()
    sid = h.id
    h.close()
    # Simula log escrito por otro harness: version futura y pasada.
    store._log._conn.execute("UPDATE session_format SET version=1 WHERE session_id=?", (sid,))
    with pytest.raises(SessionFormatUnsupportedError, match="newer harness"):
        store.open(sid)
    store._log._conn.execute("UPDATE session_format SET version=-1 WHERE session_id=?", (sid,))
    with pytest.raises(SessionFormatUnsupportedError, match="no upgrade path"):
        store.stat(sid)


def test_seq_contiguo(store):
    h = store.create()
    rowids = h.append([_msg(f"m{i}") for i in range(5)])
    assert rowids[-1] - rowids[0] + 1 == len(rowids) == 5
    assert [b - a for a, b in zip(rowids, rowids[1:])] == [1, 1, 1, 1]
    assert store.stat(h.id).event_count == 5
    h.close()


def test_doble_open_write_rechaza(store):
    h1 = store.create()
    sid = h1.id
    with pytest.raises(SessionAlreadyOwnedError):
        store.open(sid, "write")
    h1.close()
    # Tras close se libera: reabrir write OK.
    h2 = store.open(sid, "write")
    h2.close()


def test_open_missing_not_found(store):
    with pytest.raises(SessionNotFoundError):
        store.open("no-existe")
    with pytest.raises(SessionNotFoundError):
        store.stat("no-existe")


def test_read_concurrente_ok(store):
    h = store.create()
    sid = h.id
    h.append([_msg("uno"), _msg("dos")])
    r1 = store.open(sid, "read")
    r2 = store.open(sid, "read")
    assert [m.content for m in r1.read()] == ["uno", "dos"]
    assert len(r2.read(offset=1)) == 1
    # read handle no puede mutar
    with pytest.raises(SessionReadOnlyError):
        r1.append([_msg("x")])
    with pytest.raises(SessionReadOnlyError):
        r1.flush()
    h.close()


def test_flush_persiste(tmp_path):
    db = str(tmp_path / "session.db")
    s1 = SessionStore(db)
    h = s1.create()
    sid = h.id
    h.append([_msg("durable")])
    h.flush()
    h.close()
    s1.close()
    # Nueva instancia ve lo persistido (barrera cumplida).
    s2 = SessionStore(db)
    try:
        snap = s2.stat(sid)
        assert snap.event_count == 1
        r = s2.open(sid, "read")
        assert r.read()[0].content == "durable"
    finally:
        s2.close()


def test_repair_sin_truncar(store):
    h = store.create()
    sid = h.id
    h.append([
        Message(role="tool", content="begin", tool_name="turn:start"),
        _msg("trabajo a medio hacer"),
    ])
    h.close()
    before = store.stat(sid).event_count
    store.open(sid, "read")  # open corre repair del lector
    rep = store.repair_report(sid)
    assert rep is not None and rep.repaired and rep.open_turns == 1
    # Nada truncado ni borrado.
    assert store.stat(sid).event_count == before == 2


def test_handle_cerrado_rechaza(store):
    h = store.create()
    h.close()
    with pytest.raises(SessionHandleClosedError):
        h.read()
    h.close()  # idempotente, no falla
