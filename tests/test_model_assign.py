"""Model assignment — LLM por gema/ayudante según lo disponible (portable, sin ids fijos)."""
from __future__ import annotations

from src.core.model_assign import assign_model, capabilities_of, capability_for_gema
from src.core.provider_router import ModelRoute, ProviderRouter
from helpers import MockProvider


def _router(models: list[str]) -> ProviderRouter:
    r = ProviderRouter()
    r.register_provider("ollama", MockProvider("ollama", models=models))
    for m in models:
        r.register_model(ModelRoute(id=m, provider_name="ollama"))
    return r


def test_capabilities_of():
    assert "code" in capabilities_of("qwen2.5-coder:7b")
    assert "vision" in capabilities_of("qwen3-vl-8b")
    assert "reasoning" in capabilities_of("deepseek-r1:8b")
    assert "fast" in capabilities_of("nemotron-3-nano:4b")
    assert "embed" in capabilities_of("nomic-embed-text")
    assert capabilities_of("gemma4") == {"general"}


def test_capability_for_gema():
    assert capability_for_gema("code") == "code"
    assert capability_for_gema("vision") == "vision"
    assert capability_for_gema("creative") == "general"
    assert capability_for_gema("desconocida", model_hint="speed") == "fast"
    assert capability_for_gema("desconocida", model_hint="quality") == "reasoning"


def test_assign_explicit_wins():
    r = _router(["qwen2.5-coder:7b", "qwen3.5-9b"])
    a = assign_model(r, gema="code", explicit="qwen3.5-9b")
    assert a["model"] == "qwen3.5-9b"
    assert "explícito" in a["reason"]


def test_assign_capability_picks_best_installed():
    r = _router(["qwen2.5-coder:7b", "qwen3.5-9b"])
    a = assign_model(r, gema="code")
    assert a["model"] == "qwen2.5-coder:7b"
    assert "capacidad" in a["reason"]
    assert a["provider"] == "ollama"


def test_assign_capability_falls_back_to_router():
    r = _router(["deepseek-r1:8b", "nemotron-3-nano:4b"])
    a = assign_model(r, gema="code")
    assert a["model"] in ("deepseek-r1:8b", "nemotron-3-nano:4b")
    assert a["reason"].startswith("router hint")


def test_assign_override_from_config():
    r = _router(["qwen2.5-coder:7b", "qwen3.5-9b"])
    a = assign_model(r, gema="code", overrides={"code": "qwen3.5-9b"})
    assert a["model"] == "qwen3.5-9b"
    assert "override" in a["reason"]


def test_assign_override_ignored_if_not_installed():
    r = _router(["qwen2.5-coder:7b"])
    a = assign_model(r, gema="code", overrides={"code": "modelo-que-no-existe"})
    assert a["model"] == "qwen2.5-coder:7b"
    assert "capacidad" in a["reason"]


def test_assign_no_models():
    r = ProviderRouter()  # sin providers ni modelos
    a = assign_model(r, gema="code")
    assert a["model"] == ""
    assert "sin modelos" in a["reason"]
