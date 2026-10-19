"""Tests del pool de conexiones MCP (refcount, eviccion, health y reconexion)."""
import asyncio

import pytest

from src.core.mcp_connection_manager import MCPConnectionPool


class _Proc:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code


class _Srv:
    def __init__(self, proc=None):
        self.process = proc


class FakeBridge:
    """Bridge minimo con la forma que usa el pool (start/stop/_servers)."""

    def __init__(self):
        self._servers = {}
        self.started = []
        self.stopped = []

    async def start_server(self, name):
        self.started.append(name)
        self._servers[name] = _Srv()
        return object()

    async def stop_server(self, name):
        self.stopped.append(name)
        self._servers.pop(name, None)


@pytest.fixture(autouse=True)
def _reset_singleton():
    MCPConnectionPool._instance = None
    yield
    MCPConnectionPool._instance = None


def test_acquire_reusa_la_conexion_y_cuenta_refcount():
    bridge = FakeBridge()
    pool = MCPConnectionPool(bridge=bridge, max_connections=10, health_check_interval=0)

    async def scenario():
        a = await pool.acquire("alpha")
        b = await pool.acquire("alpha")
        assert a is b
        assert bridge.started == ["alpha"]
        stats = pool.get_stats()
        assert stats["active_connections"] == 1
        assert stats["connections"]["alpha"]["refcount"] == 2

        pool.release("alpha")
        assert pool.get_stats()["connections"]["alpha"]["refcount"] == 1
        pool.release("alpha")
        assert pool.get_stats()["active_connections"] == 0

    asyncio.run(scenario())


def test_release_de_servidor_inexistente_no_falla():
    pool = MCPConnectionPool(bridge=FakeBridge())
    pool.release("no-existe")  # no debe lanzar
    assert pool.get_stats()["active_connections"] == 0


def test_sin_bridge_devuelve_none():
    pool = MCPConnectionPool(bridge=None)

    async def scenario():
        assert await pool.acquire("x") is None
        assert pool.get_stats()["active_connections"] == 0

    asyncio.run(scenario())


def test_eviccion_cierra_el_servidor_evacuado():
    bridge = FakeBridge()
    pool = MCPConnectionPool(bridge=bridge, max_connections=1, health_check_interval=0)

    async def scenario():
        await pool.acquire("uno")
        await pool.acquire("dos")

    asyncio.run(scenario())

    stats = pool.get_stats()
    assert stats["active_connections"] == 1
    assert "dos" in stats["connections"]
    assert bridge.stopped == ["uno"], "el MCP evacuado no debe quedar huerfano"


def test_health_check_detecta_proceso_muerto():
    bridge = FakeBridge()
    pool = MCPConnectionPool(bridge=bridge, health_check_interval=0)

    async def scenario():
        await pool.acquire("mcp")
        ok = await pool.health_check("mcp")
        assert ok is True

        bridge._servers["mcp"].process = _Proc(exit_code=2)
        assert await pool.health_check("mcp") is False
        assert pool.get_stats()["connections"]["mcp"]["healthy"] is False

        # dentro del intervalo no vuelve a chequear (devuelve el ultimo estado)
        pool._health_check_interval = 60.0
        assert await pool.health_check("mcp") is False

    asyncio.run(scenario())


def test_health_check_de_servidor_no_pooleado_es_false():
    pool = MCPConnectionPool(bridge=FakeBridge())

    async def scenario():
        assert await pool.health_check("nope") is False

    asyncio.run(scenario())


def test_reconnect_detiene_y_reabre():
    bridge = FakeBridge()
    pool = MCPConnectionPool(bridge=bridge, health_check_interval=0)

    async def scenario():
        await pool.acquire("mcp")
        assert await pool.reconnect("mcp") is True

    asyncio.run(scenario())

    assert bridge.stopped == ["mcp"]
    assert bridge.started == ["mcp", "mcp"]
    assert pool.get_stats()["connections"]["mcp"]["reconnects"] == 1


def test_health_check_all_reconecta_los_caidos():
    bridge = FakeBridge()
    pool = MCPConnectionPool(bridge=bridge, health_check_interval=0)

    async def scenario():
        await pool.acquire("sano")
        await pool.acquire("roto")
        bridge._servers["roto"].process = _Proc(exit_code=9)
        await pool.health_check_all()

    asyncio.run(scenario())

    assert "roto" in bridge.stopped
    stats = pool.get_stats()
    assert stats["active_connections"] == 2
    assert stats["connections"]["roto"]["healthy"] is True


def test_cleanup_all_vacia_y_detiene():
    bridge = FakeBridge()
    pool = MCPConnectionPool(bridge=bridge)

    async def scenario():
        await pool.acquire("a")
        await pool.acquire("b")
        await pool.cleanup_all()

    asyncio.run(scenario())

    assert pool.get_stats()["active_connections"] == 0
    assert set(bridge.stopped) == {"a", "b"}


def test_es_singleton():
    a = MCPConnectionPool(bridge=FakeBridge())
    b = MCPConnectionPool(bridge=FakeBridge(), max_connections=1)
    assert a is b
