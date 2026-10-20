"""PASO 3 — spawner (provider/mode/descriptor, child estable, mensajería, scope, sesión) + sprint."""
from __future__ import annotations

import asyncio
import contextlib

from src.core.iteration_budget import IterationBudget
from src.core.session_log import SessionLog
from src.core.sprint_contract import CONTINUE, DONE, TAKEOVER, SprintContract
from src.core.sub_agent_spawner import (
    SubAgentSpec,
    SubAgentSpawner,
    SubAgentStatus,
    stable_child_id,
)


def _echo_executor(seen: dict):
    async def _exec(task: str, gem: str = "auto", context: str = ""):
        seen["kwargs"] = {"task": task, "gem": gem, "context": context}
        return {"ok": True, "task": task}
    return _exec


def test_provider_mode_descriptor_log_only():
    seen: dict = {}
    sp = SubAgentSpawner(executor=_echo_executor(seen))

    async def run():
        spec = SubAgentSpec(task="t1", gem="code", provider="gema", mode="continuable",
                            descriptor="plan interno: no ejecutar")
        res = await sp.spawn_child(spec)
        assert res.status == SubAgentStatus.COMPLETED
        assert (res.provider, res.mode) == ("gema", "continuable")
        # El descriptor jamás llega al executor (log-only).
        assert set(seen["kwargs"]) == {"task", "gem", "context"}
    asyncio.run(run())


def test_stable_child_id():
    seen: dict = {}
    sp = SubAgentSpawner(executor=_echo_executor(seen))

    async def run():
        r1 = await sp.spawn_child(SubAgentSpec(task="misma tarea"))
        r2 = await sp.spawn_child(SubAgentSpec(task="misma tarea"))
        assert r1.child_id == r2.child_id == stable_child_id("misma tarea", "", 1, "spawn", "one-shot")
        assert r1.child_id == r1.agent_id
    asyncio.run(run())


def test_send_interrupt_list_children():
    async def run():
        release = asyncio.Event()

        async def slow(task: str, gem: str = "auto", context: str = ""):
            # Nunca se libera: el test solo necesita que el hijo siga VIVO.
            await release.wait()
            return {"ok": True}

        sp = SubAgentSpawner(executor=slow)
        assert sp.send_message("inexistente", "hola") is False
        assert sp.interrupt("inexistente") is False
        task = asyncio.ensure_future(sp.spawn_child(SubAgentSpec(task="lento", mode="continuable")))
        # Esperar el registro sin dormir un valor fijo (antes sleep(5)+sleep(0.1)).
        for _ in range(200):
            if sp.list_children():
                break
            await asyncio.sleep(0.01)
        kids = sp.list_children()
        assert len(kids) == 1 and kids[0]["mode"] == "continuable"
        cid = kids[0]["child_id"]
        assert sp.send_message(cid, "sigue así") is True
        assert sp.get_child(cid) is not None
        assert sp.interrupt(cid) is True
        assert sp.list_children()[0]["status"] == SubAgentStatus.CANCELLED.value
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    asyncio.run(run())


def test_depth_plus_one_and_scope_never_widens():
    seen: dict = {}
    sp = SubAgentSpawner(executor=_echo_executor(seen), scope_roots={"a", "b"})

    async def run():
        parent = await sp.spawn_child(SubAgentSpec(task="padre", scope_roots={"b", "c"}))
        # Intersección: nunca se ensancha.
        assert sp.child_scope(parent.child_id) == {"b"}
        nested = sp.for_child(parent.child_id)
        assert set(nested.scope_roots) == {"b"}
        grand = await nested.spawn_child(SubAgentSpec(task="nieto", scope_roots={"b", "z"}))
        assert grand.depth == parent.depth + 1
        assert nested.child_scope(grand.child_id) == {"b"}
        assert grand.depth <= nested.max_depth
    asyncio.run(run())


def test_isolated_child_session():
    slog = SessionLog(":memory:")
    try:
        parent = slog.create_session(project="p")
        sp = SubAgentSpawner(executor=_echo_executor({}), session_log=slog,
                             parent_session_id=parent.id)

        async def run():
            res = await sp.spawn_child(SubAgentSpec(task="hijo aislado"))
            assert res.child_session_id.startswith(parent.id + "::child::")
            assert res.child_session_id != parent.id
            assert slog.get_session(res.child_session_id) is not None
        asyncio.run(run())
    finally:
        slog.close()


def test_sprint_done_condition():
    c = SprintContract(done_condition="LISTO")
    assert c.record_success("todavía no") == CONTINUE
    assert c.record_success("trabajo LISTO para entrega") == DONE
    assert c.completed


def test_sprint_three_errors_takeover_director():
    calls: list = []
    c = SprintContract(done_condition="x", max_errors=3, on_takeover=lambda *a: calls.append(a))
    assert c.record_error("e1") == CONTINUE
    assert c.record_error("e2") == CONTINUE
    assert c.record_error("e3") == TAKEOVER
    assert c.takeovers == 1 and len(calls) == 1


def test_sprint_linked_budget_exhaustion_takeovers():
    b = IterationBudget(max_iterations=1, max_tokens=10**9, max_tool_calls=10**9, max_wall_ms=10**9)
    b.tick()
    assert b.exhausted
    c = SprintContract(done_condition="x").bind_budget(b)
    assert c.check_budget() == TAKEOVER
    assert c.takeovers == 1
    # Budget sano → continue.
    c2 = SprintContract(done_condition="x").bind_budget(IterationBudget(max_iterations=99))
    assert c2.check_budget() == CONTINUE
