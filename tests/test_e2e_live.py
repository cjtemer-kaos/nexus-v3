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


def test_diagnostics_in_the_loop():
    import pathlib
    from src.core.diagnostics import diagnostics_for
    p = "./workspace/_diag_pytest.py"
    pathlib.Path(p).write_text("def f(:\n pass\n", encoding="utf-8")
    try:
        assert "file_diagnostics" in diagnostics_for(p)
    finally:
        pathlib.Path(p).unlink(missing_ok=True)
    # archivo valido -> sin diagnostico
    pathlib.Path(p).write_text("x = 1\n", encoding="utf-8")
    try:
        assert diagnostics_for(p) == ""
    finally:
        pathlib.Path(p).unlink(missing_ok=True)


def test_checkpoints_snapshot_and_restore():
    import asyncio, pathlib
    from src.api.server import _build_registry, _build_scope, _load_config_safe
    from src.core.checkpoints import list_checkpoints, restore
    cfg = _load_config_safe()
    scope = _build_scope(cfg)
    reg = _build_registry(scope, "ck-pytest", cfg)
    f = "./workspace/_ck_pytest.txt"
    pathlib.Path(f).write_text("a\nb\n", encoding="utf-8")
    try:
        asyncio.run(reg.dispatch("edit", {"path": f, "old": "b", "new": "B"}, scope=scope))
        cks = list_checkpoints("ck-pytest")
        assert cks and cks[-1]["id"]
        assert restore("ck-pytest", cks[-1]["id"])["ok"]
        assert "b" in pathlib.Path(f).read_text(encoding="utf-8")
    finally:
        pathlib.Path(f).unlink(missing_ok=True)


def test_checkpoints_endpoint():
    r = client.get("/api/checkpoints", params={"session_id": "nope"})
    assert r.status_code == 200
    assert "checkpoints" in r.json()


def test_global_search_and_catalog_endpoints():
    assert client.get("/api/sessions/global/search", params={"q": "x"}).status_code == 200
    assert client.get("/api/sessions/catalog").status_code == 200


def test_permission_gate_mutating_confirm():
    import os
    from pathlib import Path
    from src.core.scope import Scope, ScopeConfig, ScopeDenied
    sc = Scope(ScopeConfig(permission="workspace_write", require_confirm_mutating=True),
               project_root=Path(os.getcwd()))
    try:
        sc.check_tool("write_file", {"path": "./workspace/x.txt", "content": "a"})
        assert False, "deberia bloquear sin confirm"
    except ScopeDenied:
        pass
    sc.check_tool("write_file", {"path": "./workspace/x.txt", "content": "a", "confirm": True})


def test_sandbox_run_argv_policy():
    import asyncio
    from src.core.sandbox_service import SandboxService
    sb = SandboxService()
    assert asyncio.run(sb.run_argv(["git", "status"], policy="read_only")).return_code == -2
    assert asyncio.run(sb.run_argv(["python", "-c", "print(2)"], policy="read_only")).success


def test_graph_cite_and_replay_endpoints():
    sid = client.post("/api/sessions", json={"project": "r"}).json()["id"]
    assert client.get(f"/api/sessions/{sid}/replay").status_code == 200
    client.delete(f"/api/sessions/{sid}")
    g = client.get("/api/graph").json()
    nodes = g.get("nodes") or []
    if nodes and isinstance(nodes[0], dict) and nodes[0].get("id") is not None:
        r = client.get(f"/api/graph/node/{nodes[0]['id']}/cite")
        assert r.status_code == 200 and "citation" in r.json()


def test_board_claims_and_gate():
    from src.core.board import Board
    b = Board("./data/_board_pytest.db")
    t = b.create("t", priority=1)
    assert b.claim(t["id"], "a")["ok"]
    assert b.update(t["id"], status="done")["task"]["status"] == "done"
    t3 = b.create("t3")
    t4 = b.create("t4", deps=t3["id"])
    assert not b.claim(t4["id"], "a")["ok"]  # dep t3 sigue open (gate)


def test_new_endpoints_backend():
    sid = client.post("/api/sessions", json={"project": "ep"}).json()["id"]
    assert client.get("/api/events", params={"limit": 5}).status_code == 200
    assert client.get(f"/api/sessions/{sid}/tree").status_code == 200
    assert client.get(f"/api/sessions/{sid}/messages").status_code == 200
    assert client.post(f"/api/sessions/{sid}/mode", json={"mode": "plan"}).status_code == 200
    assert client.get(f"/api/sessions/{sid}/mode").json()["mode"] == "plan"
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/worktree").status_code == 200
    f = client.post(f"/api/sessions/{sid}/fork", json={}).json()
    assert f["ok"]
    assert client.delete(f"/api/sessions/{f['id']}").status_code == 200  # hijo primero (guard FK)
    assert client.delete(f"/api/sessions/{sid}").status_code == 200


def test_board_endpoints():
    t = client.post("/api/board/tasks", json={"title": "pytest-task"}).json()["task"]
    assert client.post(f"/api/board/{t['id']}/claim", json={"agent": "pytest"}).json()["ok"]
    assert client.post(f"/api/board/{t['id']}/update", json={"status": "done"}).json()["task"]["status"] == "done"


def test_diagnostics_lsp_semantic():
    import importlib.util
    import pathlib
    if not importlib.util.find_spec("pylsp"):
        return  # sin servidor LSP: se omite (best-effort)
    from src.core.diagnostics import diagnostics_for
    p = "./workspace/_lsp_pytest.py"
    pathlib.Path(p).write_text("x = undefined_name\n", encoding="utf-8")
    try:
        d = diagnostics_for(p)
        assert "file_diagnostics" in d and "undefined name" in d
    finally:
        pathlib.Path(p).unlink(missing_ok=True)


def test_sub_agent_tools_registered():
    from src.api.server import _build_registry, _build_scope, _load_config_safe
    cfg = _load_config_safe()
    scope = _build_scope(cfg)
    names = [t.name for t in _build_registry(scope, "x", cfg).list()]
    assert "sub_agent" in names and "sub_agent_message" in names and "sub_agent_list" in names


def test_mcp_server_handle():
    import asyncio
    from src.mcp_server import NexusMCPServer
    srv = NexusMCPServer()
    r = asyncio.run(srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
    assert r["result"]["serverInfo"]["name"] == "nexus-v3"
    tl = asyncio.run(srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    assert len(tl["result"]["tools"]) >= 20


def test_acp_server_handle():
    import asyncio
    from src.acp_server import NexusACPAgent
    a = NexusACPAgent()
    r = asyncio.run(a.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
    assert r["result"]["agentInfo"]["name"] == "nexus-v3"
    n = asyncio.run(a.handle({"jsonrpc": "2.0", "id": 2, "method": "session/new"}))
    assert n["result"]["sessionId"]
