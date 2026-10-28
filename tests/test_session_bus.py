"""Bus de eventos por sesion + espejo en vivo de la UI.

El turno se puede lanzar desde otro cliente (API, script, sub-agente, otra
ventana). Antes la UI solo se enteraba recargando: el unico consumidor de los
callbacks de streaming era el que habia hecho el POST. El bus publica cada
paso/delta por session_id y la UI se suscribe por SSE.
"""

from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient

from src.api.server import app
from src.core import session_bus as bus


def setup_function(_fn=None):
    bus.reset()


def test_publicar_sin_suscriptores_no_falla_ni_retiene():
    assert bus.publish("ses-x", {"type": "step"}) == 0
    assert bus.publish("", {"type": "step"}) == 0
    assert bus.publish("ses-x", {}) == 0 or True  # evento vacio no rompe
    assert bus.stats()["suscriptores"] == 0


def test_suscriptor_recibe_lo_que_publica_otro_hilo():
    """El loop del agente corre en el threadpool: el publish es cross-thread."""

    async def escenario():
        sub = bus.subscribe("ses-a")
        try:
            resultado: list = []

            def otro_hilo():
                bus.publish("ses-a", {"type": "step", "tool_used": "read_file"})
                bus.publish("ses-a", {"type": "done", "success": True})

            threading.Thread(target=otro_hilo).start()
            for _ in range(2):
                resultado.append(await asyncio.wait_for(sub.queue.get(), timeout=5))
            return resultado
        finally:
            bus.unsubscribe("ses-a", sub)

    evs = asyncio.run(escenario())
    assert [e["type"] for e in evs] == ["step", "done"]
    assert evs[0]["tool_used"] == "read_file"
    assert bus.stats()["suscriptores"] == 0


def test_buzon_acotado_no_rompe_con_suscriptor_dormido():
    async def escenario():
        sub = bus.subscribe("ses-b")
        try:
            for i in range(bus.MAX_EVENTS + 50):
                bus.publish("ses-b", {"type": "delta", "text": str(i)})
            await asyncio.sleep(0.05)
            return sub.queue.qsize()
        finally:
            bus.unsubscribe("ses-b", sub)

    assert 0 < asyncio.run(escenario()) <= bus.MAX_EVENTS


def test_suscriptores_por_sesion_tienen_tope():
    async def escenario():
        subs = [bus.subscribe("ses-c") for _ in range(bus.MAX_SUBS_POR_SESION + 3)]
        n = bus.stats()["suscriptores"]
        for s in subs:
            bus.unsubscribe("ses-c", s)
        return n

    n = asyncio.run(escenario())
    assert n == bus.MAX_SUBS_POR_SESION
    assert bus.stats()["suscriptores"] == 0


def test_hubo_actividad_marca_la_sesion():
    assert not bus.hubo_actividad("ses-d")
    bus.publish("ses-d", {"type": "start"})
    assert bus.hubo_actividad("ses-d")


def test_endpoint_events_abre_con_hello_y_entrega_los_pasos():
    """La UI necesita saber desde el primer byte si hay un run en curso, y
    despues recibir los pasos aunque el turno lo haya lanzado otro cliente."""
    from src.api.server import session_events

    async def escenario():
        resp = await session_events("ses-e2e")
        gen = resp.body_iterator
        try:
            hello = await asyncio.wait_for(gen.__anext__(), timeout=5)
            bus.publish("ses-e2e", {"type": "step", "tool_used": "write_file", "phase": "act"})
            paso = await asyncio.wait_for(gen.__anext__(), timeout=5)
            bus.publish("ses-e2e", {"type": "done", "success": True})
            fin = await asyncio.wait_for(gen.__anext__(), timeout=5)
            try:
                sobro = await asyncio.wait_for(gen.__anext__(), timeout=5)
            except StopAsyncIteration:
                sobro = None
            return hello, paso, fin, sobro
        finally:
            await gen.aclose()

    hello, paso, fin, sobro = asyncio.run(escenario())
    assert '"type": "hello"' in hello or '"type":"hello"' in hello
    assert "ses-e2e" in hello
    assert "write_file" in paso
    assert '"done"' in fin
    assert sobro is None  # el generador termina en done: EventSource no queda colgado
    assert bus.stats()["suscriptores"] == 0


def test_endpoint_events_registrado():
    rutas = {getattr(r, "path", "") for r in app.routes}
    assert "/api/sessions/{session_id}/events" in rutas
