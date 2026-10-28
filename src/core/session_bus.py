"""Bus de eventos por sesion: lo que corre fuera de la UI tambien se ve en vivo.

El turno se puede disparar desde otro cliente (la API, un script, un sub-agente,
una segunda ventana). Hasta ahora la UI solo se enteraba al recargar, porque los
callbacks de streaming los consumia unicamente el cliente que hizo el POST.

Aca se publica cada paso/delta de la sesion y cualquier suscriptor por
`session_id` los recibe por SSE. Es un pub/sub en memoria, sin dependencias:
`publish` es thread-safe (el loop del agente corre en un hilo del threadpool) y
nunca lanza, para que un suscriptor lento o muerto no corte un run.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

# Buzon acotado: si nadie lee (pestaña dormida), se descartan los viejos en vez
# de crecer sin limite. Un delta perdido no rompe nada; la UI ignora huecos.
MAX_EVENTS = 400
MAX_SUBS_POR_SESION = 8
VENTANA_ACTIVA_S = 900.0

_lock = threading.Lock()
_subs: dict[str, list["_Sub"]] = {}
_ultimo: dict[str, float] = {}


class _Sub:
    """Suscripcion: el loop donde vive la cola + la cola en si."""

    __slots__ = ("loop", "queue")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_EVENTS)


def _poner(sub: _Sub, event: dict) -> None:
    try:
        sub.queue.put_nowait(event)
    except asyncio.QueueFull:
        try:
            sub.queue.get_nowait()
            sub.queue.put_nowait(event)
        except Exception:
            pass


def publish(session_id: str, event: dict[str, Any]) -> int:
    """Publica un evento. Devuelve cuantos suscriptores lo recibieron."""
    if not session_id or not isinstance(event, dict):
        return 0
    with _lock:
        subs = list(_subs.get(session_id) or ())
        _ultimo[session_id] = time.time()
    total = 0
    for sub in subs:
        try:
            sub.loop.call_soon_threadsafe(_poner, sub, event)
            total += 1
        except (RuntimeError, AttributeError):
            pass
    return total


def subscribe(session_id: str) -> _Sub:
    """Registra una suscripcion en el loop actual (llamar desde el event loop)."""
    sub = _Sub(asyncio.get_running_loop())
    if not session_id:
        return sub
    with _lock:
        lista = _subs.setdefault(session_id, [])
        lista.append(sub)
        if len(lista) > MAX_SUBS_POR_SESION:
            del lista[0]
    return sub


def unsubscribe(session_id: str, sub: _Sub) -> None:
    with _lock:
        lista = _subs.get(session_id)
        if not lista:
            return
        if sub in lista:
            lista.remove(sub)
        if not lista:
            _subs.pop(session_id, None)


def hubo_actividad(session_id: str, ventana: float = VENTANA_ACTIVA_S) -> bool:
    """True si la sesion publico algo hace poco (run en curso o recien cerrado)."""
    with _lock:
        t = _ultimo.get(session_id)
    return bool(t and (time.time() - t) < ventana)


def stats() -> dict:
    with _lock:
        return {
            "sesiones": len(_subs),
            "suscriptores": sum(len(v) for v in _subs.values()),
            "sesiones_con_actividad": len(_ultimo),
        }


def reset() -> None:
    """Solo para tests: vacia el registro."""
    with _lock:
        _subs.clear()
        _ultimo.clear()
