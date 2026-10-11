"""Descubrimiento/cache de modelos por proveedor (provider_models)."""
import time

from src.core import provider_models as pm


def test_is_chat_model_filters_noise():
    assert pm.is_chat_model("gpt-5.3-codex")
    assert pm.is_chat_model("deepseek-v4.1-flash")
    assert pm.is_chat_model("kimi-k3")
    # ruido: embeddings / tts / guardrails
    assert not pm.is_chat_model("text-embedding-3-large")
    assert not pm.is_chat_model("canopylabs/orpheus-v1-english")
    assert not pm.is_chat_model("meta-llama/llama-prompt-guard-2-86m")
    assert not pm.is_chat_model("openai/gpt-oss-safeguard-20b")
    assert not pm.is_chat_model("whisper-large-v3")
    assert not pm.is_chat_model("")


def test_cache_roundtrip_and_ttl(tmp_path, monkeypatch):
    cache = tmp_path / "pm.json"
    monkeypatch.setenv("NEXUS_PROVIDER_MODELS_CACHE", str(cache))
    pm.save_cache({"opencode-go": {"models": ["kimi-k3", "text-embedding-x"], "ts": time.time()}})
    # filtra no-chat y devuelve solo chat
    assert pm.cached_models("opencode-go") == ["kimi-k3"]
    # vencido -> None
    pm.save_cache({"opencode-go": {"models": ["kimi-k3"], "ts": time.time() - 100_000}})
    assert pm.cached_models("opencode-go") is None
    # max_age_s=-1 ignora la edad (para routing)
    assert pm.cached_models("opencode-go", max_age_s=-1) == ["kimi-k3"]


def test_byok_ids_includes_catalog_bearer_providers():
    ids = pm.byok_ids()
    assert "opencode-go" in ids
    assert "cloud-zen" in ids
    assert "groq" in ids
    # no-bearer / local no deben estar
    assert "anthropic" not in ids
    assert "ollama" not in ids
