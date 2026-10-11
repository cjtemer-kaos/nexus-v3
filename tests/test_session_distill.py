"""Session distillation — extrae conocimiento al grafo ANTES de compactar."""
from __future__ import annotations

import asyncio

from src.core.agent_loop import AgentLoop
from src.core.provider_base import BaseProvider, LLMResponse
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_distill import build_session_digest, distill_session
from src.core.tool_registry import ToolRegistry


def _msgs():
    return [
        {"role": "user", "content": "Crea el archivo config y ejecútalo"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_name": "write_file", "content": "escrito config.py"},
        {"role": "tool", "tool_name": "exec", "content": "Error: permiso denegado"},
        {"role": "assistant", "content": "Listo: creé config.py y detecté un error de permisos."},
        {"role": "user", "content": "Arréglalo"},
        {"role": "tool", "tool_name": "exec", "content": "ok"},
        {"role": "assistant", "content": "Arreglado."},
    ]


def test_build_digest():
    d = build_session_digest("s1", _msgs())
    assert d["turns"] == 2
    assert "write_file" in d["tools"] and "exec" in d["tools"]
    assert "Crea el archivo config" in d["content"]
    assert "Error: permiso denegado" in d["content"]


def test_distill_persists_consolidated_node():
    captured = []
    def learn(content, *, topic_key="", category="general", tags=None, title=""):
        captured.append({"content": content, "topic_key": topic_key, "category": category, "tags": tags, "title": title})
    res = distill_session("s1", _msgs(), learn)
    assert res["distilled"] >= 1
    assert captured[0]["topic_key"] == "session/s1"
    assert captured[0]["category"] == "episodio"
    assert "write_file" in captured[0]["content"]


def test_distill_empty():
    res = distill_session("s0", [{"role": "system", "content": "x"}], lambda *a, **k: None)
    assert res["distilled"] == 0


class _FakeCompaction:
    def __init__(self):
        self.compacted = False
    def maybe_compact(self, msgs, max_tokens, threshold=0.75):
        return True
    async def compact(self, msgs, limit):
        self.compacted = True
        return msgs, None


class _FinalProvider(BaseProvider):
    async def chat(self, messages, tools=None, model=None, **kw):
        return LLMResponse(content="ok", model="m")
    async def health(self):
        return True
    async def list_models(self):
        return ["m"]


def test_agent_loop_distills_before_compaction():
    async def run():
        calls = []
        fc = _FakeCompaction()
        router = ProviderRouter()
        router.register_provider("mock", _FinalProvider())
        router.register_model(ModelRoute(id="m", provider_name="mock"))
        loop = AgentLoop(provider_router=router, tool_registry=ToolRegistry(),
                         compaction=fc, distill_hook=lambda sid: calls.append(sid))
        await loop.run(task="hola", session_id="sX")
        assert calls == ["sX"]          # el hook corrió
        assert fc.compacted is True      # ...antes de compactar
    asyncio.run(run())


def test_parse_json_items():
    from src.api.server import _parse_json_items
    txt = 'bla bla [{"title":"t","content":"el fix fue activar WAL","category":"fix","tags":["sqlite"]}] fin'
    items = _parse_json_items(txt)
    assert items and items[0]["content"] == "el fix fue activar WAL"
    assert items[0]["category"] == "fix" and items[0]["tags"] == ["sqlite"]
    assert _parse_json_items("no hay json") == []
    assert _parse_json_items("") == []


def test_distill_llm_extract_picks_capability_model(monkeypatch):
    import src.api.server as srv
    from src.core.provider_router import ProviderRouter, ModelRoute
    from helpers import MockProvider

    class JsonProvider(MockProvider):
        async def chat(self, messages, tools=None, model=None, **kw):
            return LLMResponse(content='[{"title":"x","content":"leccion: usar WAL en sqlite","category":"lesson"}]', model="m")

    r = ProviderRouter()
    r.register_provider("ollama", JsonProvider("ollama", models=["nemotron-3-nano:4b"]))
    r.register_model(ModelRoute(id="nemotron-3-nano:4b", provider_name="ollama"))
    monkeypatch.setattr(srv, "_get_router", lambda cfg: r)
    items = asyncio.run(srv._distill_llm_extract({}, "digest de prueba", None))
    assert items and items[0]["content"] == "leccion: usar WAL en sqlite"


def test_distill_llm_extract_fallback_on_failure(monkeypatch):
    import src.api.server as srv
    from src.core.provider_router import ProviderRouter, ModelRoute
    from helpers import MockProvider

    class FailProvider(MockProvider):
        async def chat(self, messages, tools=None, model=None, **kw):
            raise RuntimeError("modelo caido")

    r = ProviderRouter()
    r.register_provider("ollama", FailProvider("ollama", models=["gemma4"]))
    r.register_model(ModelRoute(id="gemma4", provider_name="ollama"))
    monkeypatch.setattr(srv, "_get_router", lambda cfg: r)
    assert asyncio.run(srv._distill_llm_extract({}, "digest", None)) == []


def test_near_dup_detects_similar_and_rejects_distinct():
    from src.api.server import _near_dup

    class FakeKM:
        def fts_search(self, q, limit=6):
            return [{"content": "El fix fue activar WAL en sqlite para evitar locks"}]

    assert _near_dup(FakeKM(), "activar WAL en sqlite evita los locks del fix") is True
    assert _near_dup(FakeKM(), "receta de cocina italiana con tomate y albahaca") is False
    assert _near_dup(FakeKM(), "corto") is False   # <3 palabras -> no evalua
