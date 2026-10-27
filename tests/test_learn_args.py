"""learn: los parametros que el modelo manda de verdad se perdian en silencio.

Medido: `learn({"topic": "apply_patch encoding", "content": "...", "source":
"probe", "importance": 0.1})` creaba un nodo con `topic_key: null`. `topic` es el
nombre del parametro en `brain_remember` de la colmena y `source`/`importance`
existen en `KnowledgeMemory.learn`, pero ni el schema ni ARG_ALIASES los
conocian: validate_args los descartaba sin avisar. Consecuencia: sin topic_key
no hay UPSERT, asi que cada turno dejaba un nodo duplicado en el grafo.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.tool_registry import ARG_ALIASES, validate_args  # noqa: E402

_ESQ_LEARN = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "topic_key": {"type": "string"},
        "category": {"type": "string"},
        "tags": {"type": "string"},
        "importance": {"type": "integer"},
        "source": {"type": "string"},
    },
    "required": ["content"],
    "additionalProperties": False,
}


def test_topic_se_mapea_a_topic_key():
    out = validate_args("learn", {"content": "x", "topic": "a/b"}, _ESQ_LEARN)

    assert out == {"content": "x", "topic_key": "a/b"}


def test_key_tambien_es_topic_key():
    assert validate_args("learn", {"content": "x", "key": "a/b"}, _ESQ_LEARN)["topic_key"] == "a/b"


def test_source_e_importance_no_se_descartan():
    out = validate_args("learn", {"content": "x", "source": "probe", "importance": 3}, _ESQ_LEARN)

    assert out["source"] == "probe"
    assert out["importance"] == 3


def test_aliases_no_pisan_parametros_reales():
    """`topic_key` explicito gana sobre `topic`."""
    out = validate_args("learn", {"content": "x", "topic": "viejo", "topic_key": "nuevo"}, _ESQ_LEARN)

    assert out["topic_key"] == "nuevo"
    assert "topic" not in out


@pytest.fixture()
def aprender(monkeypatch):
    from src.api import server as S
    from src.core import knowledge_memory as km

    capturado: list[dict] = []

    class _Fake:
        def learn(self, content, **kw):
            capturado.append({"content": content, **kw})
            return {"ok": True, "node_id": 1}

    monkeypatch.setattr(km, "get_knowledge_memory", lambda: _Fake())

    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, f"test-learn-{uuid.uuid4().hex[:8]}", cfg)

    def usar(args):
        raw = asyncio.run(reg.dispatch("learn", args, scope=scope, hooks=None))
        return json.loads(raw)

    return usar, capturado


def test_learn_usa_topic_y_escala_de_importance(aprender):
    usar, visto = aprender

    usar({"content": "algo durable", "topic": "probe/tema", "importance": 0.1})

    assert visto[0]["topic_key"] == "probe/tema"
    assert visto[0]["importance"] == 1  # 0.1 -> redondeo 0 -> piso 1


def test_learn_acota_importance(aprender):
    usar, visto = aprender

    usar({"content": "algo durable", "importance": 99})

    assert visto[0]["importance"] == 10


def test_learn_default_source_agent(aprender):
    usar, visto = aprender

    usar({"content": "algo durable"})

    assert visto[0]["source"] == "agent"


def test_alias_topic_sigue_en_el_mapa():
    assert ARG_ALIASES["topic"] == "topic_key"
