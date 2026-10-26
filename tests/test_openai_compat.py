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


def test_fallback_lento_se_corta_con_504(monkeypatch):
    """Un id desconocido no puede colgar minutos: pasado el presupuesto responde 504.

    Antes se esperaba lo que tardara el routing automatico (168 s medidos con un typo,
    por carga en frio de un modelo + reintento en otro proveedor).
    """
    import asyncio
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from src.api import server as srv

    async def slow_run(*a, **k):
        await asyncio.sleep(5)
        return SimpleNamespace(final_output="nunca llega", success=True)

    class FakeSlog:
        def create_session(self, project="default"):
            return SimpleNamespace(id="sess-slow")

    monkeypatch.setattr(srv, "_run_agent_once", slow_run)
    monkeypatch.setattr(srv, "_session_log", lambda: FakeSlog())
    monkeypatch.setattr(srv, "_FALLBACK_BUDGET_S", 0.05)

    r = TestClient(srv.app).post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hola"}], "model": "no-existe-xyz-123"},
    )

    assert r.status_code == 504
    detail = r.json()["detail"]
    assert detail["error"] == "model_not_found_slow_fallback"
    assert "director" in detail["valid_gemas"]
    assert detail["models_endpoint"] == "/v1/models"


def test_header_x_nexus_model_reporta_el_modelo_real(monkeypatch):
    """Con model=auto el header debe decir el modelo que respondio, no "auto".

    Antes era imposible verificar el routing desde el cliente: habia que mirar
    /api/ps de Ollama para saber que modelo estaba cargado.
    """
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from src.api import server as srv

    async def fake_run(*a, **k):
        return SimpleNamespace(final_output="ok", success=True, model="qwen3-vl-8b")

    class FakeSlog:
        def create_session(self, project="default"):
            return SimpleNamespace(id="sess-model")

    monkeypatch.setattr(srv, "_run_agent_once", fake_run)
    monkeypatch.setattr(srv, "_session_log", lambda: FakeSlog())

    r = TestClient(srv.app).post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hola"}], "model": "auto"},
    )

    assert r.status_code == 200
    assert r.headers["x-nexus-model"] == "qwen3-vl-8b"
    assert r.json()["model"] == "qwen3-vl-8b"
