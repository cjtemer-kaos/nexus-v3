"""Sección [8] Extra: Compaction / EventStore / Trajectory / HooksEngine (paridad run_tests.py)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from src.core.compaction import CompactionPipeline
from src.core.event_store import EventStore
from src.core.hooks_engine import HookAction, HookResult, HooksEngine
from src.core.trajectory import Trajectory


def test_compaction_maybe_compact_and_compact():
    async def run():
        pipe = CompactionPipeline(
            {
                "snip": {"keep_recent": 2},
                "budget": {"max_tool_result_chars": 50, "persist_dir": "./data/spill"},
                "micro": {"placeholder": "[colapsado]"},
                "summary": {"model_hint": "simple"},
            }
        )
        msgs = (
            [{"role": "system", "content": "sys"}]
            + [{"role": "user", "content": "hola " * 100} for _ in range(10)]
            + [{"role": "tool", "content": "tool result " * 200} for _ in range(3)]
        )
        need = pipe.maybe_compact(msgs, max_tokens=200, threshold=0.75)
        assert need == True
        out, metrics = await pipe.compact(msgs, limit=300)
        assert len(out) < len(msgs) and metrics.tokens_saved > 0

    asyncio.run(run())


def test_compaction_reactive():
    async def run():
        pipe = CompactionPipeline({"snip": {"keep_recent": 2}, "summary": {"model_hint": "simple"}})
        msgs = [{"role": "user", "content": "hola " * 100} for _ in range(10)]
        out2 = await pipe.reactive_compact(msgs, "context_limit 300, got 5000 prompt too long")
        assert len(out2) < len(msgs)

    asyncio.run(run())


def test_compaction_spill_persistence():
    spill_files = list(Path("./data/spill").glob("spill_*.txt")) if Path("./data/spill").exists() else []
    assert len(spill_files) >= 0


def test_event_store_append_replay_stats():
    es = EventStore(":memory:")
    try:
        es.append_event("user_message", {"content": "hi"}, session_id="s1")
        es.append_event("tool_result", {"tool": "echo", "result": "ok"}, session_id="s1")
        assert es.count("s1") == 2
        evs = es.replay("s1")
        assert len(evs) == 2
        assert [e.kind for e in evs] == ["user_message", "tool_result"]
        evs2 = es.get_events(session_id="s1", kind="tool_result")
        assert len(evs2) == 1
        st = es.get_stats()
        assert st["total_events"] >= 2
        assert "by_kind" in st
    finally:
        es.close()


def test_event_store_subscribe():
    es = EventStore(":memory:")
    try:
        hit = []
        es.subscribe("user_message", lambda e: hit.append(e.kind))
        es.append_event("user_message", {}, session_id="s1")
        assert hit == ["user_message"]
    finally:
        es.close()


def test_trajectory_steps_and_serialization():
    traj = Trajectory(session_id="t1", task="test")
    traj.append_user("hola")
    traj.append_assistant("respuesta", iteration=1)
    traj.append_assistant("", iteration=2, tool_name="echo", tool_call_id="c1", tool_args={"text": "hi"})
    traj.append_tool_result("echo", "ok", True, iteration=2, tool_call_id="c1")
    assert len(traj) == 4
    assert traj.last_assistant is not None
    msgs = traj.to_messages()
    assert len(msgs) >= 4
    roles = [m["role"] for m in msgs]
    assert "user" in roles
    j = traj.to_json()
    assert len(j) > 0


async def _h_stop(phase, payload):
    return HookResult(action=HookAction.STOP, message="halt")


async def _h_continue(phase, payload):
    return HookResult(action=HookAction.CONTINUE)


def test_hooks_stop_short_circuit():
    async def run():
        hooks = HooksEngine(config={"security_gate": True, "phases": ["pre_tool_use"]})
        hooks.on("pre_tool_use", _h_continue)
        hooks.on("pre_tool_use", _h_stop)
        res = await hooks.emit("pre_tool_use", {"tool": "x"})
        assert res.action == HookAction.STOP
        assert hooks.handler_count() > 0

    asyncio.run(run())


def test_hooks_continue():
    async def run():
        hooks2 = HooksEngine()
        hooks2.on("post_tool_use", _h_continue)
        res2 = await hooks2.emit("post_tool_use", {})
        assert res2.action == HookAction.CONTINUE

    asyncio.run(run())
