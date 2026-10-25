"""Regresion: las tools de equipo tienen que correr en el event loop.

`ToolRegistry.dispatch` manda los handlers SYNC a `asyncio.to_thread` (para que
un handler con I/O no congele el server). Eso rompe cualquier handler sync que
necesite el loop:

- `team_spawn_tool` era sync y llamaba a `TeamRuntime.spawn`, que hace
  `asyncio.create_task(...)`. En un hilo de trabajo no hay loop ->
  `RuntimeError: no running event loop`. La tool quedaba inservible.
- `team_mailbox_tool` era sync y devolvia `rt.send(...)` SIN await (send es
  `async def`), asi que `json.dumps` reventaba con "not JSON serializable"
  cada vez que se mandaba un mensaje.

El contrato es el de AGENTS.md: si el handler necesita el loop, va como
`async def`.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest


@pytest.fixture()
def registro():
    from src.api import server
    from src.core.team import TeamRuntime

    cfg = server._load_config_safe()
    sid = f"test-team-{uuid.uuid4().hex[:8]}"
    scope = server._build_scope(cfg, sid, permission_override="full")
    reg = server._build_registry(scope, sid, cfg, router=object(), helpers=True)
    yield reg
    server._TEAM_RUNTIMES.pop(sid, None)


def test_team_spawn_es_async(registro):
    """Si vuelve a ser sync, vuelve el 'no running event loop'."""
    import inspect

    entry = registro.get("team_spawn")
    assert inspect.iscoroutinefunction(entry.handler)
    assert inspect.iscoroutinefunction(registro.get("team_mailbox").handler)


def test_team_spawn_dispatch_crea_el_task(registro, monkeypatch):
    """Reproduce el fallo: el handler tiene que poder llamar create_task."""
    from src.core.team import TeamRuntime

    llamadas = []

    def spawn_falso(self, spec, name=""):
        asyncio.create_task(asyncio.sleep(0))  # lo que hace TeamRuntime.spawn
        llamadas.append(spec.task)
        return {"team_id": "team-x", "child_id": "child-x", "status": "running"}

    monkeypatch.setattr(TeamRuntime, "spawn", spawn_falso)

    async def escenario():
        return await registro.dispatch("team_spawn", {"task": "investiga algo"})

    out = json.loads(asyncio.run(escenario()))
    assert out["team_id"] == "team-x"
    assert llamadas == ["investiga algo"]


def test_team_mailbox_manda_mensaje_de_verdad(registro, monkeypatch):
    """Antes devolvia una corrutina y json.dumps explotaba."""
    from src.core.team import TeamRuntime

    async def send_falso(self, key, message):
        return {"ok": True, "team_id": key, "child_id": "child-x", "echo": message}

    monkeypatch.setattr(TeamRuntime, "send", send_falso)
    # el runtime se crea dentro del handler; basta con que exista la sesion
    monkeypatch.setattr(TeamRuntime, "list_members", lambda self: [])

    async def escenario():
        return await registro.dispatch("team_mailbox", {"team_id": "team-x", "message": "hola"})

    out = json.loads(asyncio.run(escenario()))
    assert out["ok"] is True
    assert out["echo"] == "hola"


def test_team_mailbox_sin_mensaje_lee_el_buzon(registro, monkeypatch):
    from src.core.team import TeamRuntime

    monkeypatch.setattr(TeamRuntime, "inbox", lambda self, key: {"team_id": key, "messages": ["pendiente"]})

    async def escenario():
        return await registro.dispatch("team_mailbox", {"team_id": "team-x"})

    out = json.loads(asyncio.run(escenario()))
    assert out["messages"] == ["pendiente"]
