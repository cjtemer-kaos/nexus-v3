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


def test_avisa_si_la_ventana_del_harness_supera_el_num_ctx(caplog):
    """config incoherente = turnos que mueren con 400 de Ollama; hay que avisar.

    Caso real (2026-10-26): session.max_tokens=32000 contra num_ctx=8192. La
    compactacion no saltaba hasta 24000 y todo request mayor a 8192 era rechazado.
    """
    import logging

    from src.api.server import _build_router_from_config

    cfg = {
        "session": {"max_tokens": 32000},
        "providers": {"ollama": {"base_url": "http://localhost:1", "models": [],
                                 "options": {"num_ctx": 8192}}},
        "router": {"fallback_order": ["ollama"]},
    }
    with caplog.at_level(logging.WARNING, logger="nexus-v3"):
        _build_router_from_config(cfg)

    assert any("num_ctx" in r.message for r in caplog.records), [r.message for r in caplog.records]


def test_no_avisa_si_la_ventana_es_coherente(caplog):
    import logging

    from src.api.server import _build_router_from_config

    cfg = {
        "session": {"max_tokens": 16384},
        "providers": {"ollama": {"base_url": "http://localhost:1", "models": [],
                                 "options": {"num_ctx": 16384}}},
        "router": {"fallback_order": ["ollama"]},
    }
    with caplog.at_level(logging.WARNING, logger="nexus-v3"):
        _build_router_from_config(cfg)

    assert not any("num_ctx" in r.message for r in caplog.records)


class _Modelo:
    def __init__(self, mid, prov="ollama"):
        self.id = mid
        self.provider_name = prov


class _RouterFake:
    """Router mínimo: registra qué modelo pide cada chat que hace el destilado."""

    def __init__(self, ids, prefer):
        self._ids = list(ids)
        self.prefer_models = list(prefer)
        self.llamados: list[str] = []

    def list_models(self):
        return [_Modelo(i) for i in self._ids]

    def resolve(self, task_hint=None):
        raise RuntimeError("el fake no resuelve routing")

    async def chat(self, msgs, model=None):
        self.llamados.append(model)

        class _R:
            content = "[]"

        return _R()


def test_destilado_usa_el_modelo_preferido_y_no_recarga_otro(monkeypatch):
    """El destilado corre tras CADA turno: si pide otro modelo, Ollama recarga (~45 s).

    Caso real (2026-10-26): con prefer_models=[qwen3-vl-8b] el turno usaba ese modelo
    y el destilado cargaba nemotron-3-nano:4b en cada turno, tirando abajo el que
    estaba en VRAM. El preferido tiene que ir primero.
    """
    import asyncio

    from src.api import server

    fake = _RouterFake(["qwen3-vl-8b", "nemotron-3-nano:4b"], ["qwen3-vl-8b"])
    monkeypatch.setattr(server, "_get_router", lambda cfg: fake)

    asyncio.run(server._distill_llm_extract({}, "resumen de la sesion", None))

    assert fake.llamados, "el destilado no intento ningun modelo"
    assert fake.llamados[0] == "qwen3-vl-8b", fake.llamados

