"""Compat OpenAI (/v1) y alias del cerebro tras la migracion SuperNEXUS v2 -> nexus-v3.

Cubre lo que estaba roto: /v1/chat/completions ignoraba el campo `model`, asi que un id
inventado respondia 200 como si existiera y las 24 gemas eran indistinguibles.
"""
from __future__ import annotations

import pytest


def test_gema_exacta():
    from src.api.server import _openai_model_target

    t = _openai_model_target("director")
    assert t == {"requested": "director", "gema": "director", "model": None, "fallback": False}


def test_gema_con_prefijo_nexus():
    from src.api.server import _openai_model_target

    assert _openai_model_target("nexus/code")["gema"] == "code"


@pytest.mark.parametrize("mid,gema", [
    ("supernexus/nemotron", "director"),
    ("supernexus/qwen-coder", "code"),
    ("supernexus/deepseek", "sage"),
    ("qwen-coder", "code"),
    ("deepseek-reason", "sage"),
    ("nemotron", "director"),
    ("supernexus/vision", "vision"),
])
def test_ids_heredados_de_v2(mid, gema):
    from src.api.server import _openai_model_target

    t = _openai_model_target(mid)
    assert t["gema"] == gema
    assert t["model"] is None
    assert t["fallback"] is False


@pytest.mark.parametrize("alias", ["auto", "", "nexus-v3", "nexus/auto", "nexus"])
def test_alias_auto_no_es_fallback(alias):
    from src.api.server import _openai_model_target

    t = _openai_model_target(alias)
    assert t["gema"] == ""
    assert t["model"] is None
    assert t["fallback"] is False


def test_desconocido_marca_fallback():
    from src.api.server import _openai_model_target

    t = _openai_model_target("no-existe-xyz-123")
    assert t["gema"] == ""
    assert t["model"] is None
    assert t["fallback"] is True


def test_todas_las_gemas_resuelven():
    from src.api.server import _openai_model_target
    from src.core.gema_profiles import list_gemas

    for g in list_gemas():
        t = _openai_model_target(g["name"])
        assert t["gema"] == g["name"], g["name"]
        assert t["fallback"] is False


def test_v1_models_expone_gemas_y_modelos():
    from fastapi.testclient import TestClient

    from src.api.server import app

    r = TestClient(app).get("/v1/models")
    assert r.status_code == 200
    data = r.json()["data"]
    ids = {m["id"] for m in data}
    owners = {m["id"]: m["owned_by"] for m in data}
    assert "auto" in ids
    assert "director" in ids and owners["director"] == "nexus-gema"
    assert "code" in ids and owners["code"] == "nexus-gema"
    assert len([i for i in ids if owners[i] == "nexus-gema"]) >= 24


def test_brain_stats_alias_responde_forma_v2():
    from fastapi.testclient import TestClient

    from src.api.server import app

    r = TestClient(app).get("/api/brain/stats")
    assert r.status_code == 200
    body = r.json()
    for campo in ("conocimientos", "temas", "memorias", "aristas"):
        assert campo in body
        assert isinstance(body[campo], int)
    assert body["fuente"] == "nexus-v3"
