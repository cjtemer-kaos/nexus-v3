"""Continuación real de hijos `continuable` (team_mailbox / sub_agent_message).

Defecto medido con el spawner y el server reales: `team_mailbox(message=...)`
devolvía `{"ok": true}` y el mensaje NUNCA se respondía. `send_message` solo
encola en `_inbox` y no había nadie que lo drenara: el hijo había terminado y
`team_await` repetía el resultado viejo ("Listo. Esperando instrucciones.").

Segundo defecto del mismo circuito: el spawner reportaba
`child_session_id = "{padre}::child::{child_id}"` pero el executor del server
construía `"{padre}::sub::{hash(task, context)}"`, así que el id que veía el
padre no correspondía a la sesión donde trabajaba el hijo.
"""
from __future__ import annotations

import asyncio

import pytest

from src.core.sub_agent_spawner import (
    SubAgentSpawner,
    SubAgentSpec,
    SubAgentStatus,
    stable_child_id,
)
from src.core.team import TeamRuntime


def _spawner(executor) -> SubAgentSpawner:
    return SubAgentSpawner(executor=executor, parent_session_id="padre")


def test_el_hijo_trabaja_en_la_sesion_que_reporta_el_spawner():
    async def run():
        vistos: list[str] = []

        async def executor(task, gem="auto", context="", allowed_tools=(),
                           child_session_id=""):
            vistos.append(child_session_id)
            return "ok"

        sp = _spawner(executor)
        rec = await sp.spawn_child(SubAgentSpec(task="mirá esto", mode="continuable"))

        assert rec.child_session_id == f"padre::child::{rec.child_id}"
        assert vistos == [rec.child_session_id]

    asyncio.run(run())


def test_resume_corre_otro_turno_con_el_mensaje_del_buzon():
    async def run():
        turnos: list[dict] = []

        async def executor(task, gem="auto", context="", allowed_tools=(),
                           child_session_id=""):
            turnos.append({"task": task, "gem": gem, "tools": tuple(allowed_tools),
                           "sid": child_session_id})
            return f"turno {len(turnos)}"

        sp = _spawner(executor)
        rec = await sp.spawn_child(SubAgentSpec(
            task="primer turno", gem="code", mode="continuable",
            allowed_tools=("read_file",)))

        assert sp.send_message(rec.child_id, "ahora decime OTRA COSA") is True
        assert sp.can_resume(rec.child_id) is True

        rec2 = await sp.resume_child(rec.child_id)

        assert rec2 is rec
        assert rec2.status == SubAgentStatus.COMPLETED
        assert rec2.result == "turno 2"
        assert "OTRA COSA" in turnos[1]["task"]
        assert turnos[1]["gem"] == "code"
        assert turnos[1]["tools"] == ("read_file",)
        assert turnos[1]["sid"] == rec.child_session_id
        assert sp.drain_inbox(rec.child_id) == [], "el buzón quedó sin drenar"

    asyncio.run(run())


def test_resume_acepta_el_mensaje_como_argumento():
    async def run():
        turnos: list[str] = []

        async def executor(task, **_kw):
            turnos.append(task)
            return "ok"

        sp = _spawner(executor)
        rec = await sp.spawn_child(SubAgentSpec(task="charla", mode="continuable"))
        await sp.resume_child(rec.child_id, "y ahora qué?")

        assert "y ahora qué?" in turnos[1]

    asyncio.run(run())


def test_one_shot_no_acepta_mensajes_y_lo_dice():
    async def run():
        async def executor(task, **_kw):
            return "listo"

        sp = _spawner(executor)
        rec = await sp.spawn_child(SubAgentSpec(task="una sola vez"))

        assert sp.can_resume(rec.child_id) is False
        with pytest.raises(ValueError) as e:
            await sp.resume_child(rec.child_id, "hola?")
        msg = str(e.value)
        assert "one-shot" in msg and "continuable" in msg

    asyncio.run(run())


def test_hijo_activo_rechaza_el_mensaje_y_dice_que_espere():
    async def run():
        liberar = asyncio.Event()

        async def executor(task, **_kw):
            await liberar.wait()
            return "ok"

        sp = _spawner(executor)
        task = asyncio.create_task(
            sp.spawn_child(SubAgentSpec(task="largo", mode="continuable")))
        child_id = stable_child_id("largo", "", 1, "spawn", "continuable")
        for _ in range(100):
            if child_id in sp._children:
                break
            await asyncio.sleep(0.01)
        assert child_id in sp._children
        await asyncio.sleep(0.05)

        with pytest.raises(ValueError) as e:
            await sp.resume_child(child_id, "mientras trabajás")
        assert "sigue trabajando" in str(e.value)

        liberar.set()
        await task

    asyncio.run(run())


def test_child_id_desconocido_lista_los_que_hay():
    async def run():
        async def executor(task, **_kw):
            return "ok"

        sp = _spawner(executor)
        rec = await sp.spawn_child(SubAgentSpec(task="algo", mode="continuable"))

        with pytest.raises(ValueError) as e:
            await sp.resume_child("no-existe", "hola")
        msg = str(e.value)
        assert "sub-agente desconocido" in msg
        assert rec.child_id in msg

    asyncio.run(run())


def test_resume_sin_mensajes_no_gasta_un_turno():
    async def run():
        turnos = []

        async def executor(task, **_kw):
            turnos.append(task)
            return "ok"

        sp = _spawner(executor)
        rec = await sp.spawn_child(SubAgentSpec(task="callado", mode="continuable"))

        with pytest.raises(ValueError) as e:
            await sp.resume_child(rec.child_id)
        assert "no tiene mensajes pendientes" in str(e.value)
        assert len(turnos) == 1

    asyncio.run(run())


def test_un_turno_fallido_no_quema_al_hijo():
    async def run():
        turnos: list[str] = []

        async def executor(task, **_kw):
            turnos.append(task)
            if len(turnos) == 2:
                raise RuntimeError("se cayó el modelo")
            return "ok"

        sp = _spawner(executor)
        rec = await sp.spawn_child(SubAgentSpec(task="frágil", mode="continuable"))

        fallido = await sp.resume_child(rec.child_id, "rompete")
        assert fallido.status == SubAgentStatus.FAILED
        assert "se cayó el modelo" in fallido.error

        sano = await sp.resume_child(rec.child_id, "de nuevo")
        assert sano.status == SubAgentStatus.COMPLETED
        assert sano.error == ""
        assert len(turnos) == 3

    asyncio.run(run())


def test_team_send_reanuda_al_hijo_terminado():
    async def run():
        async def executor(task, **_kw):
            return f"eco: {task}"

        rt = TeamRuntime(_spawner(executor))
        rt.spawn(SubAgentSpec(task="primer turno", mode="continuable"), name="charla")
        r1 = await rt.await_member("charla", timeout=5)
        assert r1["status"] == "completed"
        assert "primer turno" in r1["result"]["result"]

        out = await rt.send("charla", "segundo turno")
        assert out["ok"] is True and out["resumed"] is True
        assert out["resumed_count"] == 1

        r2 = await rt.await_member("charla", timeout=5)
        assert r2["status"] == "completed"
        assert "segundo turno" in r2["result"]["result"], "team_await repitió el resultado viejo"

    asyncio.run(run())


def test_team_send_mientras_trabaja_se_responde_al_terminar():
    async def run():
        liberar = asyncio.Event()
        turnos: list[str] = []

        async def executor(task, **_kw):
            turnos.append(task)
            if len(turnos) == 1:
                await liberar.wait()
            return f"eco: {task}"

        rt = TeamRuntime(_spawner(executor))
        rt.spawn(SubAgentSpec(task="largo", mode="continuable"), name="ocupado")
        await asyncio.sleep(0.05)

        out = await rt.send("ocupado", "mientras tanto")
        assert out["ok"] is True and out.get("queued") is True

        liberar.set()
        r = await rt.await_member("ocupado", timeout=5)
        assert "mientras tanto" in r["result"]["result"]
        assert len(turnos) == 2

    asyncio.run(run())


def test_team_send_a_un_one_shot_explica_el_motivo():
    async def run():
        async def executor(task, **_kw):
            return "listo"

        rt = TeamRuntime(_spawner(executor))
        rt.spawn(SubAgentSpec(task="único"), name="solo")
        await rt.await_member("solo", timeout=5)

        with pytest.raises(ValueError) as e:
            await rt.send("solo", "otra cosa")
        assert "one-shot" in str(e.value)

    asyncio.run(run())


class _FakeSpawnerSinResume:
    """Spawner viejo/fake: sin resume_child. Debe seguir funcionando igual."""

    async def spawn_child(self, spec):
        from src.core.sub_agent_spawner import SubAgentResult
        await asyncio.sleep(0)
        return SubAgentResult(agent_id="c1", task=spec.task, child_id="c1",
                              status=SubAgentStatus.COMPLETED, result="hecho")

    def send_message(self, child_id, message):
        self.caja = getattr(self, "caja", [])
        self.caja.append((child_id, message))
        return True


def test_sin_resume_child_el_envio_sigue_encolando():
    async def run():
        sp = _FakeSpawnerSinResume()
        rt = TeamRuntime(sp)
        rt.spawn(SubAgentSpec(task="charla"), name="viejo")
        await rt.await_member("viejo", timeout=5)

        out = await rt.send("viejo", "hola")

        assert out["ok"] is True and out["resumed"] is False
        assert sp.caja == [("c1", "hola")]

    asyncio.run(run())
