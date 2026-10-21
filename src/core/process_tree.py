"""Ejecucion de shell con kill del arbol de procesos.

Port del comportamiento de Cline `executors/bash.ts` (killProcessTree): matar
solo el shell deja vivos a los hijos (`node`, `pytest`, `npm`), que siguen
consumiendo CPU y reteniendo pipes. En Windows se usa `taskkill /T /F`; en
POSIX, el grupo de procesos (`start_new_session` + `killpg`).
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess


def _spawn_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except Exception:
            pass
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def run_shell_capture(
    command: str,
    cwd: str | os.PathLike,
    timeout_s: float,
) -> tuple[int, str]:
    """Corre `command` con shell y devuelve (returncode, stdout+stderr).

    Si vence el timeout, mata el arbol entero y lanza TimeoutError con la salida
    parcial (los ultimos 2000 chars suelen tener el error real).
    """
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **_spawn_kwargs(),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        except Exception:
            out = b""
        partial = (out or b"").decode("utf-8", errors="replace")[-2000:]
        detail = f"comando excedio {timeout_s}s y se mato el arbol de procesos"
        if partial.strip():
            detail += f"; salida parcial:\n{partial}"
        raise TimeoutError(detail) from None
    return int(proc.returncode or 0), (out or b"").decode("utf-8", errors="replace")
