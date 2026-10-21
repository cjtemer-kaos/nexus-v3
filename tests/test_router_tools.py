"""Routing: no elegir embeddings ni modelos sin tools; fallbacks intra-proveedor."""
from src.core.provider_router import ProviderRouter, ModelRoute, is_chat_model_id


def test_is_chat_model_id_excludes_embeddings():
    assert is_chat_model_id("qwen3.5-9b")
    assert is_chat_model_id("deepseek-r1:8b")
    assert not is_chat_model_id("nomic-embed-text")
    assert not is_chat_model_id("text-embedding-3-large")
    assert not is_chat_model_id("nomic-embed-text:latest")


def _router_with(*models):
    r = ProviderRouter(fallback_order=["ollama"])
    r.register_provider("ollama", object())
    for mid, tier in models:
        r.register_model(ModelRoute(id=mid, provider_name="ollama", tier=tier))
    return r


def test_resolve_never_picks_embedding_model():
    r = _router_with(("nomic-embed-text", 1), ("qwen3.5-9b", 3))
    out = r.resolve(task_hint="infer")
    assert out is not None
    assert out.model != "nomic-embed-text"


def test_research_rutea_a_scholar_y_no_al_director():
    # Ids reales que registra Ollama (tag con ':'), no la variante con '-'.
    r = _router_with(
        ("deepseek-r1:8b", 5), ("qwen3.5-9b", 4), ("nexus-director-v6", 7),
        ("qwen3-vl-8b", 8), ("nemotron-3-nano:4b", 1),
    )
    assert r.resolve(task_hint="research").model == "deepseek-r1:8b"
    assert r.resolve(task_hint="reasoning").model == "deepseek-r1:8b"
    assert r.resolve(task_hint="simple").model == "nemotron-3-nano:4b"


def test_attempts_skip_embeddings_and_add_fallbacks():
    r = _router_with(("nomic-embed-text", 1), ("qwen3.5-9b", 3), ("nemotron-3-nano-4b", 3))
    att = r._attempts(None, "infer", False)
    models = [m for (_p, m) in att]
    assert "nomic-embed-text" not in models
    # hay mas de un modelo del mismo proveedor como fallback
    assert len(models) >= 2
