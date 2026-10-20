"""Regresiones de la auditoría 2026-10-11 (fixes de seguridad/robustez)."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from src.core.provider_base import BaseProvider, LLMResponse
from src.core.provider_router import ModelRoute, ProviderRouter


def test_config_endpoint_masks_api_key(monkeypatch):
    """/api/config no debe exponer api_key resueltas (CRITICAL)."""
    from src.api import server as srv
    fake = {"providers": {"cloud": {"api_key": "sk-SUPERSECRET1234567890", "base_url": "x"}}}
    monkeypatch.setattr(srv, "_state", {"config": fake})
    c = TestClient(srv.app)
    r = c.get("/api/config")
    assert r.status_code == 200
    assert "sk-SUPERSECRET1234567890" not in r.text
    assert "••" in r.json()["providers"]["cloud"]["api_key"]


class _BoomAfterToken(BaseProvider):
    async def chat(self, m, tools=None, model=None, **k):
        return LLMResponse(content="x")

    async def stream(self, m, tools=None, model=None, **k):
        yield {"type": "text", "text": "A1"}
        raise RuntimeError("boom")

    async def health(self):
        return True

    async def list_models(self):
        return ["a"]


def test_router_stream_does_not_fallback_after_tokens():
    """Si un provider falla DESPUES de emitir tokens, el router re-lanza (no mezcla salidas)."""
    async def run():
        r = ProviderRouter()
        r.register_provider("a", _BoomAfterToken())
        r.register_model(ModelRoute(id="a", provider_name="a"))
        got = []
        err = None
        try:
            async for ev in r.stream([], model="a"):
                got.append(ev)
        except RuntimeError as e:
            err = str(e)
        assert got == [{"type": "text", "text": "A1"}], got
        assert err == "boom", err  # re-lanza el error ORIGINAL, no el generico
    asyncio.run(run())


def test_spawn_bg_retains_reference():
    """Las tasks background quedan referenciadas (evita GC mid-run)."""
    from src.api import server as srv

    async def run():
        srv._BG_TASKS.clear()
        async def _slow():
            await asyncio.sleep(0.01)
        srv._spawn_bg(_slow())
        assert len(srv._BG_TASKS) == 1
        # Poll en vez de sleep fijo: el done_callback corre cuando el loop cede.
        for _ in range(200):
            if not srv._BG_TASKS:
                break
            await asyncio.sleep(0.01)
        assert len(srv._BG_TASKS) == 0  # done_callback la libera
    asyncio.run(run())
