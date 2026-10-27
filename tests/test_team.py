"""Team tools: spawn no bloqueante, await, timeout y mailbox."""
from __future__ import annotations

import asyncio

from src.core.sub_agent_spawner import SubAgentResult, SubAgentSpec, SubAgentStatus
from src.core.team import TeamRuntime


class FakeSpawner:
    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.inboxes: dict[str, list[str]] = {}
        self.calls = 0

    async def spawn_child(self, spec: SubAgentSpec):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return SubAgentResult(
            agent_id="c1", task=spec.task, status=SubAgentStatus.COMPLETED,
            result="hecho", child_id="c1",
        )

    def send_message(self, child_id: str, message: str) -> bool:
        self.inboxes.setdefault(child_id, []).append(message)
        return True

    def drain_inbox(self, child_id: str):
        msgs = self.inboxes.get(child_id, [])
        self.inboxes[child_id] = []
        return msgs


def test_team_spawn_is_async_and_await_returns_result():
    async def run():
        rt = TeamRuntime(FakeSpawner())
        started = asyncio.get_running_loop().time()
        meta = rt.spawn(SubAgentSpec(task="investigar"), name="scout")
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 0.05, "team_spawn debe volver antes de que el hijo termine"
        assert meta["team_id"] == "scout"
        assert meta["status"] == "running"

        res = await rt.await_member("scout", timeout=5)
        assert res["ok"] and res["status"] == "completed"
        assert res["result"]["result"] == "hecho"

    asyncio.run(run())


def test_team_await_timeout_does_not_cancel_child():
    async def run():
        rt = TeamRuntime(FakeSpawner(delay=0.3))
        rt.spawn(SubAgentSpec(task="lento"), name="slow")
        first = await rt.await_member("slow", timeout=0.05)
        assert first["timed_out"] is True
        second = await rt.await_member("slow", timeout=5)
        assert second["status"] == "completed"

    asyncio.run(run())


def test_team_mailbox_send_and_read():
    async def run():
        rt = TeamRuntime(FakeSpawner())
        rt.spawn(SubAgentSpec(task="charla"), name="chat")
        await rt.await_member("chat", timeout=5)
        sent = await rt.send("chat", "hola")
        assert sent["ok"]
        box = rt.inbox("chat")
        assert box["messages"] == ["hola"]
        assert rt.inbox("chat")["messages"] == []

    asyncio.run(run())


def test_team_list_members():
    async def run():
        rt = TeamRuntime(FakeSpawner())
        rt.spawn(SubAgentSpec(task="a"), name="uno")
        rt.spawn(SubAgentSpec(task="b"), name="dos")
        assert {m["team_id"] for m in rt.list_members()} == {"uno", "dos"}
        await asyncio.gather(*(rt.await_member(t, 5) for t in ("uno", "dos")))

    asyncio.run(run())


def test_id_desconocido_dice_cuales_hay():
    """Sin la lista, el modelo reintenta con otro id inventado."""
    async def run():
        rt = TeamRuntime(FakeSpawner())
        solo = await rt.await_member("no-existe", timeout=1)
        assert solo["ok"] is False
        assert "No hay equipos activos" in solo["error"]

        rt.spawn(SubAgentSpec(task="a"), name="scout")
        con_equipo = rt.inbox("no-existe")
        assert "Activos: scout" in con_equipo["error"]
        assert rt.inbox("no-existe")["error"].count("no-existe") == 1
        await rt.await_member("scout", timeout=5)

    asyncio.run(run())

