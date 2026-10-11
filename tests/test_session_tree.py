"""Sesiones en arbol (pi-style): fork / branch / tree sobre el SessionLog existente."""
from __future__ import annotations

from src.core.session_log import Message, SessionLog


def test_fork_branch_tree():
    slog = SessionLog(":memory:")
    try:
        root = slog.create_session(project="tree")
        slog.append(root.id, Message(role="user", content="hola"))
        child = slog.fork(root.id)
        assert child.id != root.id

        t = slog.tree(child.id)
        assert t["session"] == child.id and t["exists"]
        assert root.id in t["ancestors"]
        assert child.id in slog.tree(root.id)["children"]

        b = slog.branch(root.id, copy_messages=True)
        assert len(slog.get_messages(b.id, limit=10)) == 1
        b2 = slog.branch(root.id, copy_messages=False)
        assert len(slog.get_messages(b2.id, limit=10)) == 0
    finally:
        slog.close()
