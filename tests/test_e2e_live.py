"""E2E in-process (TestClient): cubre flujos que antes se escapaban
(slash commands, routing sin embeddings, tools, FS unidades, providers)."""
from fastapi.testclient import TestClient

from src.api.server import app
from src.core.provider_router import is_chat_model_id

client = TestClient(app)


def test_slash_commands_present():
    # comandos anunciados por la UI + alias español
    for cmd in ["/herramientas", "/alcance", "/permiso", "/modelo", "/ayudantes",
                "/help", "/compact", "/todo"]:
        r = client.post("/api/slash", json={"command": cmd, "ctx": {}})
        assert r.status_code == 200
        msg = r.json().get("message") or ""
        assert "unknown slash" not in msg, f"{cmd}: {msg}"


def test_routing_never_picks_embedding():
    from src.api.server import _build_router_from_config, _load_config_safe
    r = _build_router_from_config(_load_config_safe())
    for h in ["infer", "none", "coding", "vision"]:
        rt = r.resolve(task_hint=h)
        assert rt is not None, h
        assert is_chat_model_id(rt.model), f"{h} -> {rt.model}"


def test_tools_registry_complete():
    from src.api.server import _build_registry, _build_scope, _load_config_safe, _build_router_from_config
    cfg = _load_config_safe()
    reg = _build_registry(_build_scope(cfg), "t", cfg, router=_build_router_from_config(cfg))
    tools = reg.to_openai_tools()
    names = [t["function"]["name"] for t in tools]
    for n in ["read_file", "write_file", "edit", "list_dir", "exec", "gema_run", "agent_run",
              "gema_list", "agent_list", "learn", "recall", "web_search", "mcp_list", "mcp_call"]:
        assert n in names, n
    # schemas validos + tipos integer (from __future__ annotations)
    for t in tools:
        assert (t.get("function", {}).get("parameters") or {}).get("type") == "object"
    by = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
    assert by["exec"]["properties"]["timeout"]["type"] == "integer"
    # atributo para la delegacion con imagen
    assert hasattr(reg, "current_images")


def test_edit_tool_str_replace():
    import asyncio, pathlib
    from src.api.server import _build_registry, _build_scope, _load_config_safe
    cfg = _load_config_safe()
    scope = _build_scope(cfg)
    reg = _build_registry(scope, "t-edit", cfg)
    f = "./workspace/_edit_pytest.txt"
    pathlib.Path(f).write_text("uno\ndos\ntres\n", encoding="utf-8")
    try:
        r = asyncio.run(reg.dispatch("edit", {"path": f, "old": "dos", "new": "DOS"}, scope=scope))
        assert "editado" in str(r)
        assert "DOS" in pathlib.Path(f).read_text(encoding="utf-8")
    finally:
        pathlib.Path(f).unlink(missing_ok=True)


def test_fs_lists_drives_root():
    r = client.get("/api/fs/list", params={"path": ""})
    assert r.status_code == 200
    assert isinstance(r.json().get("entries"), list)


def test_providers_no_cloud_zen():
    r = client.get("/api/providers")
    assert r.status_code == 200
    assert "cloud-zen" not in (r.json().get("providers") or {})


def test_sessions_delete_roundtrip():
    sid = client.post("/api/sessions", json={"project": "pytest-e2e"}).json()["id"]
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.delete(f"/api/sessions/{sid}").status_code == 404
