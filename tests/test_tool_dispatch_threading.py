"""Regresion: un handler sync lento no puede congelar el event loop.

`ToolRegistry.dispatch` ejecutaba los handlers sync en linea. Con tools como
job_wait (duerme hasta 300 s esperando un proceso), web_fetch (HTTP) o
read_file/write_file (disco), durante esa llamada no avanzaba ningun SSE,
ni /api/status, ni el keepalive: el server entero quedaba mudo.

El arreglo manda los handlers sync a un hilo (asyncio.to_thread). Estos tests
fijan el contrato: el loop sigue vivo mientras el handler trabaja, el resultado
y las excepciones se propagan igual, y el gate de scope corre en el loop.
"""
from __future__ import annotations

import asyncio
import threading
import time

from src.core.tool_registry import ToolRegistry


def _reg_con(reg, fn, nombre="lento"):
    reg.register_function(fn, name=nombre, description="handler sync lento", category="local")


def test_handler_sync_no_bloquea_el_event_loop():
    reg = ToolRegistry()

    def duerme(segundos: float = 0.4) -> str:
        time.sleep(segundos)
        return "listo"

    _reg_con(reg, duerme)

    async def escenario():
        latido = {"ticks": 0, "vivo_mientras": False}

        async def ticker():
            while True:
                await asyncio.sleep(0.02)
                latido["ticks"] += 1

        tarea = asyncio.create_task(ticker())
        try:
            resultado = await reg.dispatch("lento", {"segundos": 0.4})
            # el ticker tiene que haber avanzado durante los 400 ms del handler
            latido["vivo_mientras"] = latido["ticks"] >= 5
        finally:
            tarea.cancel()
        return resultado, latido

    resultado, latido = asyncio.run(escenario())
    assert resultado == "listo"
    assert latido["vivo_mientras"], f"el loop quedo bloqueado (ticks={latido['ticks']})"


def test_handler_sync_corre_en_otro_hilo():
    reg = ToolRegistry()
    hilos: list[str] = []

    def quiensoy() -> str:
        hilos.append(threading.current_thread().name)
        return "ok"

    _reg_con(reg, quiensoy, nombre="quiensoy")

    async def escenario():
        principal = threading.current_thread().name
        await reg.dispatch("quiensoy", {})
        return principal

    principal = asyncio.run(escenario())
    assert hilos and hilos[0] != principal


def test_handler_sync_propaga_excepciones_y_metricas():
    reg = ToolRegistry()

    def falla() -> str:
        raise ValueError("boom")

    _reg_con(reg, falla, nombre="falla")

    async def escenario():
        try:
            await reg.dispatch("falla", {})
        except ValueError as e:
            return str(e)
        return None

    assert asyncio.run(escenario()) == "boom"
    entry = reg.get("falla")
    assert entry.call_count == 1
    assert entry.success_count == 0
    assert "boom" in entry.last_error


def test_handler_sync_respeta_el_scope():
    reg = ToolRegistry()

    class ScopeQueNiega:
        def check_tool(self, name, args):
            raise PermissionError("denegado por scope")

    def nunca() -> str:
        raise AssertionError("no deberia ejecutarse")

    _reg_con(reg, nunca, nombre="nunca")

    async def escenario():
        try:
            await reg.dispatch("nunca", {}, scope=ScopeQueNiega())
        except PermissionError as e:
            return str(e)
        return None

    assert asyncio.run(escenario()) == "denegado por scope"
