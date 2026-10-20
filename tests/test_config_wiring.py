"""Cableado de config: las claves declaradas deben tener efecto real.

Cubre los hallazgos de la 6a pasada de auditoria:
- scope.max_read_bytes / max_file_read_bytes (effective_max_bytes ignoraba uno)
- embeddings.embed_url (ignoraba OLLAMA_HOST y el parser de config)
- checkpoints / provider_models no deben depender del CWD
- AgentLoop debe tomar session.max_tokens, compact_threshold, heartbeat y error_policy
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_scope_effective_max_bytes_honra_ambos_nombres():
    from src.core.scope import ScopeConfig

    assert ScopeConfig(max_read_bytes=2_000_000).effective_max_bytes() == 2_000_000
    assert ScopeConfig(max_file_read_bytes=3_000_000).effective_max_bytes() == 3_000_000
    # si ambos estan seteados, gana el nombre explicito
    assert ScopeConfig(max_read_bytes=2_000_000,
                       max_file_read_bytes=3_000_000).effective_max_bytes() == 3_000_000


def test_build_scope_usa_el_maximo_configurado():
    from src.api.server import _build_scope

    scope = _build_scope({"scope": {"max_read_bytes": 2_500_000}}, permission_override="full")
    assert scope.cfg.effective_max_bytes() == 2_500_000
    scope2 = _build_scope({"scope": {"max_file_read_bytes": 3_500_000}}, permission_override="full")
    assert scope2.cfg.effective_max_bytes() == 3_500_000


def test_embed_url_respeta_ollama_host(monkeypatch):
    from src.core.embeddings import embed_url

    monkeypatch.setenv("OLLAMA_HOST", "192.0.2.10:11500")
    assert embed_url() == "http://192.0.2.10:11500"


def test_checkpoints_viven_en_el_proyecto(monkeypatch):
    from src.core import checkpoints

    monkeypatch.delenv("NEXUS_CHECKPOINTS_DIR", raising=False)
    assert checkpoints._ROOT.is_absolute()
    assert checkpoints._ROOT == _ROOT / "data" / "checkpoints"


def test_provider_models_cache_vive_en_el_proyecto(monkeypatch):
    from src.core import provider_models

    monkeypatch.delenv("NEXUS_PROVIDER_MODELS_CACHE", raising=False)
    assert provider_models._cache_path().is_absolute()
    assert provider_models._cache_path() == _ROOT / "data" / "provider_models.json"


def test_new_agent_loop_aplica_config():
    from src.api.server import _new_agent_loop

    cfg = {
        "session": {"max_tokens": 12345, "compact_threshold": 0.5},
        "agent_loop": {
            "max_iterations": 7,
            "heartbeat_ms": 1500,
            "error_policy": {"retry": 1, "replan": 0},
            "budget": {"max_iterations": 7, "max_tokens": 900, "max_wall_ms": 1000,
                       "max_tool_calls": 3},
        },
    }
    loop = _new_agent_loop(cfg)
    assert loop.context_max_tokens == 12345
    assert loop.compact_threshold == 0.5
    assert loop.heartbeat_ms == 1500
    assert loop._error_policy == {"retry": 1, "replan": 0}
    assert loop.budget.max_iterations == 7
    assert loop.budget.max_tool_calls == 3


def test_agent_loop_sin_config_usa_defaults():
    from src.api.server import _new_agent_loop

    loop = _new_agent_loop({})
    assert loop.context_max_tokens == 32000
    assert loop.compact_threshold == 0.75
    assert loop.heartbeat_ms == 2000
