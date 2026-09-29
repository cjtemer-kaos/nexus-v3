"""Sección [3] Provider routing (mock) (paridad run_tests.py)."""
from __future__ import annotations

import asyncio

import pytest
from src.core.provider_base import LLMMessage
from src.core.provider_router import HINT_MAPPING, ModelRoute, ProviderRouter
from helpers import MockProvider, MockToolProvider


@pytest.fixture(scope="module")
def router():
    r = ProviderRouter(vram_budget_gb=12, fallback_order=["llamacpp", "ollama", "cloud"], strategy="tier")
    r.register_provider("llamacpp", MockProvider("llamacpp", models=["nexus-director-v6", "qwen3.5-9b"]))
    r.register_provider("ollama", MockProvider("ollama", models=["qwen2.5-coder:7b", "deepseek-r1:8b"]))
    r.register_provider("cloud", MockProvider("cloud", models=["muse-spark-1.2"]))
    r.register_model(ModelRoute(id="nexus-director-v6", provider_name="llamacpp", tier=7, vram_gb=4.3))
    r.register_model(ModelRoute(id="qwen2.5-coder:7b", provider_name="ollama", tier=3, vram_gb=4.0))
    r.register_model(ModelRoute(id="muse-spark-1.2", provider_name="cloud", tier=5))
    return r


def test_registration(router):
    assert "llamacpp" in router.providers()
    assert len(router._models) == 3


def test_resolve_simple(router):
    r = router.resolve(task_hint="simple")
    assert r and r.route.effective_tier() >= 1


def test_resolve_deep(router):
    r2 = router.resolve(task_hint="deep")
    if r2:
        r2.route.provider_name


def test_resolve_coding(router):
    r3 = router.resolve(task_hint="coding")
    if r3:
        r3.model


def test_preferred_model(router):
    r4 = router.resolve(preferred_model="muse-spark-1.2")
    assert r4 and r4.model == "muse-spark-1.2"


def test_chat_fallback(router):
    async def run():
        router2 = ProviderRouter(fallback_order=["bad", "good"])
        router2.register_provider("bad", MockProvider("bad", fail=True))
        router2.register_provider("good", MockProvider("good", models=["good-model"]))
        router2.register_model(ModelRoute(id="good-model", provider_name="good", tier=1))
        resp = await router2.chat([LLMMessage(role="user", content="hola")], hint="simple")
        assert "good" in (resp.content or "")

        resp2 = await router.chat([LLMMessage(role="user", content="test tier router")], hint="simple")
        assert resp2.content is not None

    asyncio.run(run())


def test_thompson_strategy():
    async def run():
        router3 = ProviderRouter(strategy="thompson", fallback_order=["llamacpp", "ollama"])
        router3.register_provider("llamacpp", MockProvider("llamacpp", models=["m-a"]))
        router3.register_provider("ollama", MockProvider("ollama", models=["m-b"]))
        router3.register_model(ModelRoute(id="m-a", provider_name="llamacpp", tier=3))
        router3.register_model(ModelRoute(id="m-b", provider_name="ollama", tier=3))
        r_th = router3.resolve(task_hint="coding", strategy="thompson")
        assert r_th is not None

    asyncio.run(run())


def test_vram_strategy():
    router4 = ProviderRouter(strategy="vram")
    router4.register_provider("a", MockProvider("a"))
    router4.register_provider("b", MockProvider("b"))
    router4.register_model(ModelRoute(id="heavy", provider_name="a", tier=5, vram_gb=8))
    router4.register_model(ModelRoute(id="light", provider_name="b", tier=5, vram_gb=1))
    r_vram = router4.resolve(task_hint="reasoning", strategy="vram")
    assert r_vram is not None and r_vram.model == "light"


def test_auto_detect():
    async def run():
        router5 = ProviderRouter()
        router5.register_provider("mock_ok", MockProvider("mock_ok", healthy=True, models=["auto-model"]))
        router5.register_provider("mock_bad", MockProvider("mock_bad", healthy=False))
        ad = await router5.auto_detect()
        assert ad.get("mock_ok") == True and ad.get("mock_bad") == False
        has_auto = any(m.id == "auto-model" for m in router5._models)
        assert has_auto

    asyncio.run(run())


def test_health_all(router):
    async def run():
        ha = await router.health_all()
        assert "llamacpp" in ha or "mock_ok" in ha

    asyncio.run(run())


def test_stats(router):
    router.stats()["strategy"]


def test_hint_mapping_exists():
    assert HINT_MAPPING is not None
