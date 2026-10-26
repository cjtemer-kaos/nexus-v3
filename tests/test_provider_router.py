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


def test_prefer_models_gana_al_mapeo_por_gema():
    """router.prefer_models unifica el modelo: un multimodal atiende todos los hints.

    Motivo: cada cambio de modelo en Ollama cuesta ~45 s de recarga en frio. La gema
    conserva su system prompt; solo cambia el modelo que la ejecuta.
    """
    r = ProviderRouter(fallback_order=["ollama"], strategy="tier",
                       prefer_models=["qwen3-vl-8b", "qwen2.5vl:7b"])
    r.register_provider("ollama", MockProvider("ollama", models=["qwen3-vl-8b", "qwen3.5-9b"]))
    r.register_model(ModelRoute(id="qwen3.5-9b", provider_name="ollama", tier=4))
    r.register_model(ModelRoute(id="qwen3-vl-8b", provider_name="ollama", tier=8))

    for hint in ("none", "coding", "reasoning", "vision", "planning", "simple"):
        got = r.resolve(task_hint=hint)
        assert got is not None, hint
        assert got.model == "qwen3-vl-8b", (hint, got.model)


def test_prefer_models_respeta_model_explicito():
    """Un `model=` explicito sigue ganando sobre la preferencia."""
    r = ProviderRouter(fallback_order=["ollama"], strategy="tier",
                       prefer_models=["qwen3-vl-8b"])
    r.register_provider("ollama", MockProvider("ollama", models=["qwen3-vl-8b", "deepseek-r1:8b"]))
    r.register_model(ModelRoute(id="qwen3-vl-8b", provider_name="ollama", tier=8))
    r.register_model(ModelRoute(id="deepseek-r1:8b", provider_name="ollama", tier=5))

    got = r.resolve(task_hint="coding", preferred_model="deepseek-r1:8b")
    assert got is not None and got.model == "deepseek-r1:8b"


def test_prefer_models_vacio_mantiene_el_mapeo_por_gema():
    """Sin preferencia, el modelo sale de GEMA_MAPPING (comportamiento previo)."""
    r = ProviderRouter(fallback_order=["ollama"], strategy="tier")
    r.register_provider("ollama", MockProvider("ollama", models=["qwen3.5-9b", "qwen3-vl-8b"]))
    r.register_model(ModelRoute(id="qwen3.5-9b", provider_name="ollama", tier=4))
    r.register_model(ModelRoute(id="qwen3-vl-8b", provider_name="ollama", tier=8))

    assert r.resolve(task_hint="vision").model == "qwen3-vl-8b"
    assert r.resolve(task_hint="coding").model != "qwen3-vl-8b"


# ---- overflow de contexto: no es "modelo roto" ----

@pytest.mark.parametrize("msg,overflow", [
    # mensaje real de Ollama (body que antes se perdia en raise_for_status)
    ("ollama 400 (qwen3-vl-8b): {\"error\":\"request (10132 tokens) exceeds "
     "the available context size (8192 tokens), try increasing it\"}", True),
    ("Client error '400 Bad Request' for url 'http://localhost:11434/api/chat'", False),
    ("This model's maximum context length is 8192 tokens", True),
    ("ollama 500 (x): internal error", False),
    ("socket closed", False),
])
def test_deteccion_de_overflow_de_contexto(msg, overflow):
    from src.core.provider_router import es_overflow_de_contexto

    assert es_overflow_de_contexto(msg) is overflow


class _OverflowProvider(MockProvider):
    """Simula el 400 de Ollama: el prompt no entra en num_ctx."""

    def __init__(self, name, model):
        super().__init__(name, models=[model])
        self.model = model
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        raise RuntimeError(
            f'ollama 400 ({model}): {{"error":"request (10132 tokens) exceeds the '
            f'available context size (8192 tokens), try increasing it"}}')


def test_overflow_de_contexto_no_recorre_la_cadena_de_modelos():
    """Un prompt demasiado grande debe volver al loop para compactar, no saltar de modelo.

    Antes el router lo tomaba como fallo del modelo y probaba el resto del catalogo:
    cada salto costaba una recarga en frio (~45 s) y el turno terminaba respondiendo
    sin tools por el camino "chat degradado sin tools" (100-165 s medidos).
    """
    r = ProviderRouter(fallback_order=["ollama"], strategy="tier")
    p = _OverflowProvider("ollama", "qwen3-vl-8b")
    r.register_provider("ollama", p)
    for mid in ("qwen3-vl-8b", "nexus-director-v6", "deepseek-r1:8b"):
        r.register_model(ModelRoute(id=mid, provider_name="ollama", tier=6))

    with pytest.raises(RuntimeError, match="exceeds the available context size"):
        asyncio.run(r.chat([LLMMessage(role="user", content="hola")], hint="research"))
    assert p.calls == 1, f"deberia intentar una sola vez, intento {p.calls}"


def test_cadena_de_fallback_acotada():
    """Ante un fallo comun no se recorren los 10 modelos del catalogo (45 s cada uno)."""
    r = ProviderRouter(fallback_order=["ollama"], strategy="tier")
    p = MockProvider("ollama", fail=True)
    r.register_provider("ollama", p)
    for i in range(8):
        r.register_model(ModelRoute(id=f"modelo-{i}", provider_name="ollama", tier=i))

    attempts = r._attempts(None, "coding", False)
    assert len(attempts) <= 1 + 2, f"cadena sin tope: {[a[1] for a in attempts]}"


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
