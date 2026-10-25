"""Los jobs no deben sobrevivir al cierre de la app.

`JobRegistry.start` usa `CREATE_NEW_PROCESS_GROUP` (a propósito: un Ctrl+C en el
harness no debe cortar el build). El costo es que el proceso hijo ya no muere con
el padre: cerrar Electron dejaba `npm run dev`, pytest o un servidor de larga
duración corriendo en background, invisibles para el usuario.

El cierre ahora es explícito: `kill_all()` desde el shutdown del server.
"""
from __future__ import annotations

import asyncio
import sys

import pytest


def _dormir(segundos: int = 60) -> str:
    return f"ping -n {segundos} 127.0.0.1" if sys.platform == "win32" else f"sleep {segundos}"


def test_kill_all_termina_los_jobs_vivos(tmp_path):
    from src.core.jobs import JobRegistry

    reg = JobRegistry()
    uno = reg.start(_dormir(), cwd=tmp_path, label="dormir-1")
    dos = reg.start(_dormir(), cwd=tmp_path, label="dormir-2")
    assert reg.get(uno["id"])["state"] == "running"
    assert reg.get(dos["id"])["state"] == "running"

    assert reg.kill_all() == 2

    for jid in (uno["id"], dos["id"]):
        proc = reg._get_rec(jid)["proc"]
        proc.wait(timeout=10)
        assert proc.poll() is not None


def test_kill_all_no_cuenta_los_que_ya_terminaron(tmp_path):
    from src.core.jobs import JobRegistry

    reg = JobRegistry()
    job = reg.start("echo listo", cwd=tmp_path, label="rapido")
    reg._get_rec(job["id"])["proc"].wait(timeout=20)

    assert reg.kill_all() == 0


def test_el_shutdown_del_server_llama_a_kill_all():
    """Contrato: si alguien borra esa llamada, los jobs vuelven a quedar huérfanos."""
    import inspect

    from src.api import server

    fuente = inspect.getsource(server._shutdown)
    assert "kill_all" in fuente
