"""/api/tools debe publicar el JSON Schema de cada tool.

Antes exponia {name, description, category}: un cliente sabia que existia
`read_file` pero no que recibe `path`/`offset`/`limit`, asi que no podia armar
el prompt ni validar argumentos (opencode si publica el esquema).
"""
from __future__ import annotations


def test_tools_expone_parameters_para_todas():
    from fastapi.testclient import TestClient

    from src.api.server import app

    r = TestClient(app).get("/api/tools")
    assert r.status_code == 200
    tools = r.json()["tools"]
    assert tools
    for t in tools:
        assert set(t) >= {"name", "description", "category", "parameters", "source"}
        assert isinstance(t["parameters"], dict), t["name"]


def test_read_file_publica_su_esquema_real():
    from fastapi.testclient import TestClient

    from src.api.server import app

    tools = {t["name"]: t for t in TestClient(app).get("/api/tools").json()["tools"]}
    rf = tools["read_file"]
    props = rf["parameters"]["properties"]
    assert "path" in props
    assert "offset" in props and "limit" in props
    assert rf["parameters"]["required"] == ["path"]


def test_skill_tool_tiene_su_esquema():
    from fastapi.testclient import TestClient

    from src.api.server import app

    tools = {t["name"]: t for t in TestClient(app).get("/api/tools").json()["tools"]}
    assert "skill" in tools
    assert {"query", "name", "limit"} <= set(tools["skill"]["parameters"]["properties"])


def test_esquema_coincide_con_to_openai():
    """/api/tools y el formato OpenAI de tools salen del mismo registro."""
    from src.api.server import _build_registry, _build_scope
    from src.core.config import load_config

    reg = _build_registry(_build_scope(load_config()), "api")
    entry = next(t for t in reg.list() if t.name == "exec")
    api_shape = entry.to_dict()
    oa = entry.to_openai()
    assert oa["type"] == "function"
    assert oa["function"]["name"] == api_shape["name"]
    assert oa["function"]["parameters"] == api_shape["parameters"]
