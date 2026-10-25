"""Regresion: los routers retirados no pueden quedar abiertos para siempre.

`_get_router` cachea por firma de config: cada cambio de proveedor/modelo crea
un router nuevo y el viejo se apilaba en `_RETIRED_ROUTERS`, que solo se
recorria en el shutdown. Cada router tiene AsyncClient httpx con su pool, asi
que sesiones largas con cambios de modelo acumulaban sockets y handles.

`_schedule_router_close` (que ya existia, con el delay de 30 s para no matar
requests en vuelo) no lo llamaba nadie: era codigo muerto. Ahora `_retire_router`
hace las dos cosas: cierra diferido y saca el router de la lista al cerrarlo.
"""
from __future__ import annotations

import asyncio

import pytest


class _Prov:
    def __init__(self) -> None:
        self.cerrado = 0

    async def aclose(self) -> None:
        self.cerrado += 1


class _Router:
    def __init__(self) -> None:
        self.p = _Prov()

    def providers(self) -> dict:
        return {"falso": self.p}


@pytest.fixture()
def limpio(monkeypatch):
    from src.api import server

    monkeypatch.setattr(server, "_RETIRE_DELAY_S", 0.0)
    server._RETIRED_ROUTERS.clear()
    yield server
    server._RETIRED_ROUTERS.clear()


def test_router_retirado_se_cierra_y_se_olvida(limpio):
    r = _Router()

    async def escenario():
        limpio._retire_router(r, delay_s=0)
        await asyncio.sleep(0.05)

    asyncio.run(escenario())
    assert r.p.cerrado == 1
    assert limpio._RETIRED_ROUTERS == []


def test_retirar_sin_loop_cierra_en_el_acto(limpio):
    r = _Router()
    limpio._retire_router(r, delay_s=30)  # no hay loop corriendo
    assert r.p.cerrado == 1
    assert limpio._RETIRED_ROUTERS == []


def test_invalidate_router_no_acumula(limpio):
    r = _Router()
    limpio._state["router"] = r
    limpio._state["router_sig"] = "vieja"

    async def escenario():
        limpio._invalidate_router()
        await asyncio.sleep(0.05)

    try:
        asyncio.run(escenario())
        assert r.p.cerrado == 1
        assert limpio._RETIRED_ROUTERS == []
    finally:
        limpio._state.pop("router", None)
        limpio._state.pop("router_sig", None)


def test_mientras_espera_sigue_en_la_lista_para_el_shutdown(limpio):
    """El delay protege los requests en vuelo; el shutdown igual lo cierra."""
    r = _Router()

    async def escenario():
        limpio._retire_router(r, delay_s=30)
        en_lista = r in limpio._RETIRED_ROUTERS
        assert r.p.cerrado == 0
        # cancelar la espera simula el shutdown: el router sigue en la lista
        for t in list(limpio._CLOSE_TASKS):
            t.cancel()
        await asyncio.gather(*list(limpio._CLOSE_TASKS), return_exceptions=True)
        return en_lista

    try:
        assert asyncio.run(escenario()) is True
        assert r in limpio._RETIRED_ROUTERS
    finally:
        limpio._RETIRED_ROUTERS.clear()
        limpio._CLOSE_TASKS.clear()
