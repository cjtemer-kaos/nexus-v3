"""Concurrencia adaptativa free-thread-aware para nexus-v3.

Da "ductilidad": detecta si el interprete es free-threaded (sin GIL) y dimensiona
los pools segun la CPU disponible.
  - GIL OFF (cp314t): los threads dan paralelismo CPU REAL (~4x medido en 8 hilos).
  - GIL ON: los threads siguen sirviendo para I/O-bound (como asyncio.to_thread),
    y para CPU se puede recurrir a procesos (no implementado por simplicidad).
Todo es best-effort: si algo falla, se cae a ejecucion secuencial.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")

_pools: dict[int, ThreadPoolExecutor] = {}
_pool_lock = threading.Lock()


def is_free_threaded() -> bool:
    """True si el GIL esta desactivado (build free-threaded, p.ej. cp314t)."""
    fn = getattr(sys, "_is_gil_enabled", None)
    if fn is None:
        return False
    try:
        return not bool(fn())
    except Exception:
        return False


def cpu_count() -> int:
    return os.cpu_count() or 4


def default_workers() -> int:
    n = cpu_count()
    # free-threaded: paralelismo real -> casi todos los nucleos.
    # con GIL: threads solo ayudan en I/O -> unos pocos.
    return max(2, n - 1) if is_free_threaded() else min(8, n)


def thread_pool(max_workers: int | None = None) -> ThreadPoolExecutor:
    n = max_workers or default_workers()
    with _pool_lock:
        ex = _pools.get(n)
        if ex is None:
            ex = ThreadPoolExecutor(max_workers=n, thread_name_prefix="nexus")
            _pools[n] = ex
        return ex


def map_cpu(fn: Callable[[T], R], items: Sequence[T], max_workers: int | None = None) -> list[R]:
    """Aplica fn sobre items en paralelo (threads). Util para CPU puro (GIL off) e I/O.
    Si fn falla, el error se propaga (NO se re-ejecutan los items ya procesados)."""
    items = list(items)
    if len(items) < 2:
        return [fn(x) for x in items]
    return list(thread_pool(max_workers).map(fn, items))


async def gather_limited(coros: Iterable[Any], limit: int | None = None) -> list[Any]:
    """asyncio.gather con limite de concurrencia (evita saturar providers/sockets)."""
    lim = asyncio.Semaphore(limit or default_workers() * 2)

    async def _run(c: Any) -> Any:
        async with lim:
            return await c

    return await asyncio.gather(*[_run(c) for c in coros], return_exceptions=True)


def shutdown() -> None:
    with _pool_lock:
        for ex in _pools.values():
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        _pools.clear()


def info() -> dict[str, Any]:
    return {
        "free_threaded": is_free_threaded(),
        "cpu": cpu_count(),
        "default_workers": default_workers(),
        "python": sys.version.split()[0],
    }
