"""Regresiones de la auditorÃ­a 2026-10-25.

Cada test acÃ¡ falla con el cÃ³digo anterior y pasa con el arreglo. No son tests
de estilo: son los cuatro casos donde el harness podÃ­a mentir o morir callado.
"""
from __future__ import annotations

import asyncio
import logging

import pytest


# -- hive_runner: arrancar sin registro no es un NameError ---------------------

def test_hive_runner_sin_registro_arranca_vacio(tmp_path, monkeypatch, caplog):
    from src.bridges import hive_runner

    monkeypatch.setattr(hive_runner, "HIVE_CONFIG", tmp_path / "no-existe.json")
    with caplog.at_level(logging.WARNING):
        reg = hive_runner.load_registry()

    assert reg == {"agents": {}, "version": "2.0"}
    assert any("hive_agents.json" in r.message for r in caplog.records)


def test_hive_runner_warning_nombra_el_campo(monkeypatch):
    from src.bridges import hive_runner

    monkeypatch.setattr(hive_runner, "_LEGACY_PROJECT_PATHS", ("/legacy/nexus-ia",))
    reg = {"agents": {"aider": {"cwd": "/legacy/nexus-ia/src", "args": ["--x"]}}}
    warnings = hive_runner._validate_registry(reg)

    assert len(warnings) == 1
    # El f-string viejo decÃ­a `{field}` (nombre inexistente): el mensaje salÃ­a
    # con la palabra "field" en vez del campo real y nadie sabÃ­a quÃ© corregir.
    assert "cwd=" in warnings[0]
    assert "field" not in warnings[0]


# -- runtime_guard: Python viejo corta con un mensaje accionable ---------------

def test_runtime_guard_acepta_la_version_minima():
    from src.core.runtime_guard import python_ok, require_python

    assert python_ok((3, 11, 0)) is True
    assert python_ok((3, 14, 7)) is True
    require_python((3, 11, 0))


def test_runtime_guard_corta_si_es_viejo():
    from src.core.runtime_guard import require_python

    with pytest.raises(RuntimeError) as err:
        require_python((3, 9, 18))
    msg = str(err.value)
    assert "3.11" in msg and "3.9" in msg


# -- _spawn_bg: una sola definiciÃ³n y no explota sin loop ---------------------

def test_spawn_bg_es_unico_en_server():
    """/api/server.py definÃ­a _spawn_bg dos veces: la segunda tapaba a la
    primera y cualquier arreglo en la de arriba quedaba muerto."""
    import inspect

    from src.api import server as srv

    src = inspect.getsource(srv)
    assert src.count("def _spawn_bg(") == 1


def test_spawn_bg_sin_loop_no_deja_la_coroutine_suelta():
    from src.api import server as srv

    cerrada = {"n": 0}

    async def _trabajo():
        cerrada["n"] += 1

    coro = _trabajo()
    srv._spawn_bg(coro)  # no hay event loop corriendo
    assert cerrada["n"] == 0
    assert coro.cr_frame is None  # close() la cerrÃ³
    assert len(srv._BG_TASKS) == 0


# -- hooks_engine.emit_sync: avisa cuando saltea handlers async ---------------

def test_emit_sync_avisa_que_salteo_handlers_async(caplog):
    from src.core.hooks_engine import HookAction, HookPhase, HookResult, HooksEngine

    engine = HooksEngine()

    async def _async_hook(phase, payload):
        return HookResult(action=HookAction.CONTINUE)

    def _sync_hook(phase, payload):
        return HookResult(action=HookAction.CONTINUE)

    engine.on(HookPhase.PRE_TOOL_USE.value, _async_hook)
    engine.on(HookPhase.PRE_TOOL_USE.value, _sync_hook)

    async def _dentro_de_un_loop():
        with caplog.at_level(logging.WARNING):
            return engine.emit_sync(HookPhase.PRE_TOOL_USE.value, {})

    res = asyncio.run(_dentro_de_un_loop())

    assert res.action is HookAction.CONTINUE
    avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert avisos, "un hook async salteado en silencio es un hook perdido"
    assert "async" in avisos[0].getMessage()


def test_emit_sync_sin_async_no_avisa(caplog):
    from src.core.hooks_engine import HookAction, HookPhase, HookResult, HooksEngine

    engine = HooksEngine()
    engine.on(HookPhase.PRE_TOOL_USE.value,
              lambda phase, payload: HookResult(action=HookAction.CONTINUE))

    async def _dentro_de_un_loop():
        with caplog.at_level(logging.WARNING):
            return engine.emit_sync(HookPhase.PRE_TOOL_USE.value, {})

    asyncio.run(_dentro_de_un_loop())
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# -- cdp.navigate: navega la pestaÃ±a que dice y mira la respuesta -------------

@pytest.fixture()
def browser():
    from src.tools.cdp_browser import CdpBrowser

    b = CdpBrowser(browser_url="http://127.0.0.1:9222")
    b._pages = {"PAGINA-1": {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/PAGINA-1"}}
    b._state.active_page_id = "PAGINA-1"
    return b


def _preparar(browser, monkeypatch, respuesta):
    from src.tools.cdp_browser import CdpResult

    llamadas = []

    async def _refresh():
        return None

    async def _send(ws_url, method, params=None):
        llamadas.append((ws_url, method, params))
        return respuesta

    async def _ready(timeout=5.0):
        return None

    monkeypatch.setattr(browser, "_refresh_pages", _refresh)
    monkeypatch.setattr(browser, "_send_cdp", _send)
    monkeypatch.setattr(browser, "_wait_for_page_ready", _ready)
    return llamadas


def test_navigate_usa_page_navigate_sobre_la_pestana_activa(browser, monkeypatch):
    from src.tools.cdp_browser import CdpResult

    llamadas = _preparar(browser, monkeypatch, CdpResult(success=True, data={"frameId": "F1"}))
    res = asyncio.run(browser.navigate_page("https://example.com"))

    assert res.success is True
    metodo = [c for c in llamadas if c[1] == "Page.navigate"]
    assert len(metodo) == 1
    assert metodo[0][0].endswith("/PAGINA-1")
    assert metodo[0][2] == {"url": "https://example.com"}
    assert res.data["frame_id"] == "F1"


def test_navigate_reporta_error_de_chrome(browser, monkeypatch):
    from src.tools.cdp_browser import CdpResult

    _preparar(browser, monkeypatch,
              CdpResult(success=True, data={"errorText": "net::ERR_NAME_NOT_RESOLVED"}))
    res = asyncio.run(browser.navigate_page("https://no-existe.invalid"))

    # El cÃ³digo viejo devolvÃ­a success=True sobre la pestaÃ±a vieja: la tool
    # decÃ­a "navegaste" sin haber navegado.
    assert res.success is False
    assert "ERR_NAME_NOT_RESOLVED" in res.error


def test_navigate_propaga_fallo_del_transporte(browser, monkeypatch):
    from src.tools.cdp_browser import CdpResult

    _preparar(browser, monkeypatch, CdpResult(success=False, error="websocket cerrado"))
    res = asyncio.run(browser.navigate_page("https://example.com"))

    assert res.success is False
    assert "websocket cerrado" in res.error
