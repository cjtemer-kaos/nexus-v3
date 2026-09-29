"""
Rate Limiter — sliding window in-memory.
Adaptado del patrón Hermes (rate-limit.ts).
Sin deps externas. Cleanup automático cada 5min.
"""

import time
import logging
from typing import Dict, List
from aiohttp import web

logger = logging.getLogger(__name__)

_store: Dict[str, List[float]] = {}
_last_cleanup = time.monotonic()
_CLEANUP_INTERVAL = 300  # 5 min


def _cleanup(window_ms: float):
    global _last_cleanup
    now = time.monotonic()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    cutoff = now - window_ms / 1000
    for key in list(_store.keys()):
        _store[key] = [t for t in _store[key] if t > cutoff]
        if not _store[key]:
            del _store[key]


def rate_limit(key: str, max_requests: int, window_ms: float) -> bool:
    """
    Check if a request is allowed under the rate limit.
    Returns True if allowed, False if blocked.
    """
    now = time.monotonic()
    window_sec = window_ms / 1000
    cutoff = now - window_sec

    _cleanup(window_ms)

    if key not in _store:
        _store[key] = []

    entry = _store[key]
    entry[:] = [t for t in entry if t > cutoff]

    if len(entry) >= max_requests:
        return False

    entry.append(now)
    return True


def get_client_ip(request) -> str:
    """Extract client IP from request. Honors X-Forwarded-For via TRUST_PROXY."""
    trust = (getattr(request.app, "_trust_proxy", False) or
             (request.app.get("_trust_proxy") if isinstance(request.app, dict) else False) or
             False)
    if trust:
        forwarded = request.headers.get("X-Forwarded-For", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    peername = request.transport.get_extra_info("peername")
    if peername:
        return peername[0]
    return "local"


def rate_limit_middleware(max_requests: int = 100, window_ms: float = 60_000):
    """
    aiohttp middleware factory.
    Por defecto: 100 requests por ventana de 60s por IP.
    """
    @web.middleware
    async def middleware(request: web.Request, handler):
        key = f"ratelimit:{get_client_ip(request)}"
        if not rate_limit(key, max_requests, window_ms):
            return web.json_response(
                {"error": "Too Many Requests", "retry_after_ms": int(window_ms)},
                status=429,
            )
        return await handler(request)
    return middleware
