"""Sección [1] Session creation and message handling (paridad run_tests.py)."""
from __future__ import annotations

import threading

import pytest
from src.core.session_log import Message, SessionLog


@pytest.fixture
def slog(tmp_path):
    s = SessionLog(str(tmp_path / "session.db"), wal=True, fts=True)
    yield s
    s.close()


@pytest.fixture
def session(slog):
    return slog.create_session(project="nexus-v3")


@pytest.fixture
def session_with_msgs(slog, session):
    for role, content in [
        ("user", "Hola Nexus"),
        ("assistant", "Hola, soy Nexus v3"),
        ("tool", "resultado tool mock"),
        ("user", "segundo mensaje"),
    ]:
        slog.append(session.id, Message(role=role, content=content, tokens=len(content) // 4))
    return session


def test_wal_mode(slog):
    jmode = slog.check_wal()
    assert jmode


def test_session_creation(slog):
    sess = slog.create_session(project="nexus-v3")
    assert len(sess.id) == 12
    assert sess.project == "nexus-v3"


def test_session_append_4_msgs(slog, session_with_msgs):
    assert slog.count_messages(session_with_msgs.id) == 4


def test_get_messages_ordering(slog, session_with_msgs):
    msgs = slog.get_messages(session_with_msgs.id)
    assert msgs[0].content == "Hola Nexus"


def test_fts_search(slog, session_with_msgs):
    hits = slog.search("Nexus", limit=10)
    assert len(hits) >= 1


def test_fts_search_session(slog, session_with_msgs):
    hits2 = slog.search_session(session_with_msgs.id, "Hola")
    assert isinstance(hits2, list)


def test_get_recent_messages(slog, session_with_msgs):
    recent = slog.get_recent_messages(session_with_msgs.id, n=2)
    assert len(recent) > 0
    recent[-1].content[:20]


def test_list_filtering(slog, session_with_msgs):
    slog.create_session(project="other")
    lst = slog.list_sessions(project="nexus-v3")
    assert any(s.id == session_with_msgs.id for s in lst)


def test_get_session(slog, session_with_msgs):
    fetched = slog.get_session(session_with_msgs.id)
    assert fetched and fetched.id == session_with_msgs.id


def test_fork_branching(slog, session_with_msgs):
    child = slog.fork(session_with_msgs.id)
    assert child.parent_id == session_with_msgs.id


def test_fork_inherits_summary(slog, session_with_msgs):
    slog.compact_mark(session_with_msgs.id, "resumen padre")
    child2 = slog.fork(session_with_msgs.id)
    assert child2.summary == "resumen padre"


def test_branch_copy_messages(slog, session_with_msgs):
    br = slog.branch(session_with_msgs.id, copy_messages=True)
    assert slog.count_messages(br.id) == 4


def test_compact_mark(slog, session_with_msgs):
    slog.compact_mark(session_with_msgs.id, "final summary")
    s_after = slog.get_session(session_with_msgs.id)
    assert s_after.compact_count >= 1
    assert s_after.summary == "final summary"


def test_stats_wal_fts_rebuild(slog, session_with_msgs):
    st = slog.get_stats()
    assert st["sessions"] >= 1
    assert st["messages"] >= 4
    assert slog.check_wal()
    assert slog.check_fts()
    slog.rebuild_fts()


def test_delete_leaf_cascade(slog, session_with_msgs):
    br = slog.branch(session_with_msgs.id, copy_messages=True)
    assert slog.delete_session(br.id) == True
    assert slog.get_session(br.id) is None
    still = slog.get_session(session_with_msgs.id)
    assert still is not None


def test_delete_parent_with_children_fk_guard(slog, session_with_msgs):
    slog.fork(session_with_msgs.id)
    with pytest.raises(Exception):
        slog.delete_session(session_with_msgs.id)


def test_memory_mode(tmp_path):
    slog2 = SessionLog(":memory:")
    try:
        s = slog2.create_session()
        slog2.append(s.id, Message(role="user", content="mem test"))
        count = slog2.count_messages(s.id)
        jmode = slog2.check_wal()
        assert count == 1
        assert jmode
    finally:
        slog2.close()


def test_thread_safe_concurrent_append():
    slog3 = SessionLog(":memory:")
    try:
        sess_t = slog3.create_session()

        def writer(n):
            for i in range(20):
                slog3.append(sess_t.id, Message(role="user", content=f"thread {n} msg {i}"))

        ths = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        assert slog3.count_messages(sess_t.id) == 60
    finally:
        slog3.close()
