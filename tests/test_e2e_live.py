"""E2E in-process (TestClient): cubre flujos que antes se escapaban
(slash commands, routing sin embeddings, tools, FS unidades, providers)."""
from fastapi.testclient import TestClient

from pathlib import Path

from src.api.server import app
from src.core.provider_router import is_chat_model_id

client = TestClient(app)

# Raiz del repo por ubicacion del archivo (no depende del cwd de pytest)
_ROOT = Path(__file__).resolve().parents[1]


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
              "agent_create", "gema_list", "agent_list", "learn", "recall", "web_search", "mcp_list", "mcp_call"]:
        assert n in names, n
    # schemas validos + tipos integer (from __future__ annotations)
    for t in tools:
        assert (t.get("function", {}).get("parameters") or {}).get("type") == "object"
    by = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
    assert by["exec"]["properties"]["timeout"]["type"] == "integer"
    # atributo para la delegacion con imagen
    assert hasattr(reg, "current_images")


def test_agent_templates_endpoint():
    r = client.get("/api/agents/templates")
    assert r.status_code == 200
    data = r.json()
    names = {t["name"] for t in data.get("templates", [])}
    assert {"scout", "builder", "reviewer", "qa", "researcher", "ops"} <= names
    assert "build" in data.get("modes", [])
    assert "read_only" in data.get("modes", [])


def test_edit_tool_str_replace():
    import asyncio, pathlib
    from helpers import remove_path, write_fresh
    from src.api.server import _build_registry, _build_scope, _load_config_safe
    cfg = _load_config_safe()
    scope = _build_scope(cfg)
    reg = _build_registry(scope, "t-edit", cfg)
    f = "./workspace/_edit_pytest.txt"
    write_fresh(f, "uno\ndos\ntres\n")
    try:
        r = asyncio.run(reg.dispatch("edit", {"path": f, "old": "dos", "new": "DOS"}, scope=scope))
        assert "editado" in str(r)
        assert "```diff" in str(r) and "-2: dos" in str(r) and "+2: DOS" in str(r)
        assert "DOS" in pathlib.Path(f).read_text(encoding="utf-8")
    finally:
        remove_path(f)


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
    from helpers import remove_path, write_fresh
    from src.core.diagnostics import diagnostics_for
    p = "./workspace/_diag_pytest.py"
    write_fresh(p, "def f(:\n pass\n")
    try:
        assert "file_diagnostics" in diagnostics_for(p)
    finally:
        remove_path(p)
    # archivo valido -> sin diagnostico
    write_fresh(p, "x = 1\n")
    try:
        assert diagnostics_for(p) == ""
    finally:
        remove_path(p)


def test_checkpoints_snapshot_and_restore():
    import asyncio, pathlib
    from helpers import remove_path, write_fresh
    from src.api.server import _build_registry, _build_scope, _load_config_safe
    from src.core.checkpoints import list_checkpoints, restore
    cfg = _load_config_safe()
    scope = _build_scope(cfg)
    reg = _build_registry(scope, "ck-pytest", cfg)
    f = "./workspace/_ck_pytest.txt"
    write_fresh(f, "a\nb\n")
    try:
        asyncio.run(reg.dispatch("edit", {"path": f, "old": "b", "new": "B"}, scope=scope))
        cks = list_checkpoints("ck-pytest")
        assert cks and cks[-1]["id"]
        assert restore("ck-pytest", cks[-1]["id"])["ok"]
        assert "b" in pathlib.Path(f).read_text(encoding="utf-8")
    finally:
        remove_path(f)


def test_checkpoints_endpoint():
    r = client.get("/api/checkpoints", params={"session_id": "nope"})
    assert r.status_code == 200
    assert "checkpoints" in r.json()


def test_global_search_and_catalog_endpoints():
    assert client.get("/api/sessions/global/search", params={"q": "x"}).status_code == 200
    assert client.get("/api/sessions/catalog").status_code == 200


def test_permission_gate_mutating_confirm():
    from src.core.scope import Scope, ScopeConfig, ScopeDenied
    sc = Scope(ScopeConfig(permission="workspace_write", require_confirm_mutating=True),
               project_root=_ROOT)
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


def test_plugins_loader():
    from src.core.plugins import list_plugins, load_plugins
    from src.core.tool_registry import ToolRegistry
    root = _ROOT
    assert any(p["name"] == "hello_plugin" for p in list_plugins(root))
    reg = ToolRegistry()
    assert "hello_plugin" in load_plugins(reg, root)
    assert reg.has("hello")


def test_cost_estimate():
    from src.core.cost import estimate_cost, cost_by_model
    assert estimate_cost(1000, "qwen2.5-coder:7b") == 0.0
    assert estimate_cost(1000, "gpt-5-codex") > 0.0
    c = cost_by_model([{"model": "qwen2.5-coder:7b", "tokens": 1000},
                       {"model": "x-cloud", "tokens": 1000}])
    assert c["total_usd"] > 0 and len(c["per_model"]) == 2


def test_usage_plugins_worktree_endpoints():
    u = client.get("/api/usage")
    assert u.status_code == 200 and "by_model" in u.json()
    assert client.get("/api/plugins").status_code == 200
    assert client.post("/api/plugins/reload").status_code == 200
    assert client.post("/api/worktree/remove", json={}).status_code == 400  # path requerido


def test_agents_md_hierarchical():
    from src.core.agents_md import find_agents_md, system_block
    root = _ROOT
    assert "AGENTS.md" in [r for r, _ in find_agents_md([], root)]
    rels = [r for r, _ in find_agents_md([str(root / "src" / "core" / "x.py")], root)]
    assert "AGENTS.md" in rels and "src/core/AGENTS.md" in rels
    assert "src/core/AGENTS.md" in system_block([str(root / "src" / "core" / "x.py")], root)


def test_approvals_flow():
    import asyncio, pathlib
    from helpers import remove_path
    from src.api.server import _build_registry, _build_scope, _load_config_safe
    from src.core.approvals import get_approvals
    cfg = _load_config_safe()
    cfg.setdefault("agent_loop", {})["auto_apply"] = "approve"
    scope = _build_scope(cfg)
    reg = _build_registry(scope, "appr-pytest", cfg)
    f = "./workspace/_appr_pytest.txt"
    remove_path(f)
    try:
        out = asyncio.run(reg.dispatch("write_file", {"path": f, "content": "x"}, scope=scope))
        assert "pendiente de aprobacion" in out
        assert not pathlib.Path(f).exists()  # NO se aplicó
        items = get_approvals().list("appr-pytest")
        assert items and items[0]["tool"] == "write_file"
        res = get_approvals().resolve(items[0]["id"], True)
        assert res["ok"] and pathlib.Path(f).read_text(encoding="utf-8") == "x"
    finally:
        remove_path(f)


def test_approvals_and_plan_endpoints():
    r = client.get("/api/approvals")
    assert r.status_code == 200 and "mode" in r.json()
    p = client.post("/api/plan", json={"task": "x"})
    assert p.status_code == 200  # ok True o error (si no hay LLM), nunca crash


def test_router_adaptive_unified():
    from src.core.provider_router import ProviderRouter, ModelRoute, _get_adaptive
    from src.core.adaptive_router import AdaptiveRouter, get_adaptive_router
    assert _get_adaptive() is get_adaptive_router()  # una sola fuente de verdad
    assert ProviderRouter()._adaptive is get_adaptive_router()

    class P:
        pass

    ar = AdaptiveRouter()
    r = ProviderRouter(strategy="thompson", adaptive_router=ar)
    r.register_provider("ollama", P())
    r.register_model(ModelRoute(id="m_a", provider_name="ollama", tier=3))
    r.register_model(ModelRoute(id="m_b", provider_name="ollama", tier=3))
    for _ in range(15):
        ar.record_result("m_a", success=True)
        ar.record_result("m_b", success=False)
    assert ar.mean_model("m_a") > ar.mean_model("m_b")
    assert r.resolve(task_hint="coding", strategy="thompson").model == "m_a"
    assert r.stats().get("adaptive") is not None
    r._record_success("ollama", "m_c")  # el router alimenta el AdaptiveRouter
    assert ar.mean_model("m_c") > 0.5


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
    # El modo persiste entre reinicios: el test no debe dejar la sesión en plan.
    assert client.post(f"/api/sessions/{sid}/mode", json={"mode": "act"}).status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/worktree").status_code == 200
    f = client.post(f"/api/sessions/{sid}/fork", json={}).json()
    assert f["ok"]
    assert client.delete(f"/api/sessions/{f['id']}").status_code == 200  # hijo primero (guard FK)
    assert client.delete(f"/api/sessions/{sid}").status_code == 200


def test_board_endpoints():
    t = client.post("/api/board/tasks", json={"title": "pytest-task"}).json()["task"]
    c = client.post(f"/api/board/{t['id']}/claim", json={"agent": "pytest"}).json()
    assert c["ok"] and c["task"]["lease_expires_at"] > 0
    r = client.post(f"/api/board/{t['id']}/claim", json={"agent": "otro"}).json()
    assert not r["ok"] and "ya reclamada" in r["error"]
    assert client.post(f"/api/board/{t['id']}/update", json={"status": "done"}).json()["task"]["status"] == "done"


def test_diagnostics_lsp_semantic():
    import importlib.util
    from helpers import remove_path, write_fresh
    if not importlib.util.find_spec("pylsp"):
        return  # sin servidor LSP: se omite (best-effort)
    from src.core.diagnostics import diagnostics_for
    p = "./workspace/_lsp_pytest.py"
    write_fresh(p, "x = undefined_name\n")
    try:
        d = diagnostics_for(p)
        assert "file_diagnostics" in d and "undefined name" in d
    finally:
        remove_path(p)


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


def test_board_and_stream_input_validation():
    assert client.post("/api/board/tasks", json={}).status_code == 400
    assert client.post("/api/board/tasks", json={"title": "x", "priority": "alta"}).status_code == 400
    assert client.post("/api/board/no-existe/update", json={"status": "inventado"}).status_code == 400
    assert client.post("/api/board/no-existe/claim", json={"lease_s": "mucho"}).status_code == 400
    assert client.post("/api/checkpoints/restore", json={}).status_code == 400
    assert client.post("/api/checkpoints/restore", json={"checkpoint_id": "no-existe"}).status_code == 404
    assert client.post("/v1/chat/completions", json={}).status_code == 400
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 400
    assert client.post("/v1/chat/completions", json={"messages": [{"role": "system", "content": "x"}]}).status_code == 400
    assert client.post("/api/chat/stream", json={}).status_code == 400
    assert client.get("/api/chat/stream", params={"message": ""}).status_code == 400
    assert client.post("/api/sessions/x/messages").status_code == 400


def test_goals_endpoint_roundtrip():
    import uuid
    sid = f"pytest-goals-{uuid.uuid4().hex[:8]}"
    assert client.get("/api/goals").status_code == 400
    assert client.post("/api/goals", json={}).status_code == 400
    r = client.get("/api/goals", params={"session_id": sid})
    assert r.status_code == 200 and r.json()["exists"] is False
    r = client.post("/api/goals", json={"session_id": sid, "objective": "terminar el harness"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["goal"]["objective"] == "terminar el harness"
    r = client.get("/api/goals", params={"session_id": sid})
    assert r.json()["objective"] == "terminar el harness" and r.json()["status"] == "active"
    assert client.post("/api/goals", json={"session_id": sid, "objective": "  "}).status_code == 400
    r = client.post("/api/goals", json={"session_id": sid, "clear": True})
    assert r.json()["cleared"] is True
    assert client.get("/api/goals", params={"session_id": sid}).json()["exists"] is False


def test_slash_goal_persists_in_session():
    import uuid
    sid = f"pytest-goal-{uuid.uuid4().hex[:8]}"
    body = client.post("/api/slash", json={"command": "/goal terminar el harness profesional",
                                           "ctx": {"session_id": sid}}).json()
    assert body["ok"] is True and "goal guardado" in body["message"]
    assert client.get("/api/goals", params={"session_id": sid}).json()["objective"] == "terminar el harness profesional"
    st = client.post("/api/slash", json={"command": "/goal status", "ctx": {"session_id": sid}}).json()
    assert "terminar el harness profesional" in st["message"]
    cl = client.post("/api/slash", json={"command": "/goal clear", "ctx": {"session_id": sid}}).json()
    assert "goal borrado" in cl["message"]
    assert client.get("/api/goals", params={"session_id": sid}).json()["exists"] is False


def test_slash_goal_without_session_is_not_persisted():
    body = client.post("/api/slash", json={"command": "/goal objetivo sin sesion", "ctx": {}}).json()
    assert "no persistido" in body["message"]


def test_session_branching_error_codes():
    assert client.get("/api/sessions/no-existe-xyz/tree").status_code == 404
    assert client.post("/api/sessions/no-existe-xyz/fork", json={}).status_code == 404
    assert client.post("/api/sessions/no-existe-xyz/rewind", json={}).status_code == 400
    sid = client.post("/api/sessions", json={"project": "pytest"}).json()["id"]
    try:
        assert client.post(f"/api/sessions/{sid}/rewind", json={}).status_code == 400
        assert client.post(f"/api/sessions/{sid}/rewind", json={"message_id": "x"}).status_code == 400
        r = client.post(f"/api/sessions/{sid}/rewind", json={"message_id": 999999})
        assert r.status_code == 200 and r.json()["deleted"] == 0
        t = client.get(f"/api/sessions/{sid}/tree")
        assert t.status_code == 200 and t.json()["exists"] is True
    finally:
        client.delete(f"/api/sessions/{sid}")


def test_plugins_reload_reports_what_loaded():
    r = client.post("/api/plugins/reload")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    for k in ("dir", "available", "loaded", "errors", "failed"):
        assert k in body
    assert "hello_plugin" in body["available"]
    assert "hello_plugin" in body["loaded"]
    assert body["errors"] == [] and body["failed"] == []


def test_approvals_unknown_id_is_404():
    assert client.post("/api/approvals/no-existe/approve").status_code == 404
    assert client.post("/api/approvals/no-existe/reject").status_code == 404


def test_worktree_endpoint_rejects_non_repo():
    import shutil, tempfile
    d = tempfile.mkdtemp(prefix="n3-wt-")
    try:
        r = client.post("/api/worktree", json={"repo": d, "path": d + "/wt", "branch": "x"})
        assert r.status_code == 400
        assert "no es repo" in r.json()["detail"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_embed_input_validation():
    assert client.post("/api/embed", json={"texts": [1, 2]}).status_code == 400
    assert client.post("/api/embed", json={"texts": "  "}).status_code == 400
    assert client.post("/api/embed", content=b"[]",
                       headers={"Content-Type": "application/json"}).status_code == 400


def test_global_search_includes_session_id():
    sid = client.post("/api/sessions", json={"project": "pytest-search"}).json()["id"]
    try:
        client.post(f"/api/sessions/{sid}/messages",
                    json={"role": "user", "content": "aguja unica 7731 en el pajar"})
        r = client.get("/api/sessions/global/search", params={"q": "aguja 7731"})
        assert r.status_code == 200
        results = r.json()["results"]
        assert results, "la busqueda global no devolvio el mensaje recien escrito"
        assert results[0]["session_id"] == sid
        assert results[0]["role"] == "user"
    finally:
        client.delete(f"/api/sessions/{sid}")


def test_session_branch_accepts_copy_messages_in_body():
    """El body JSON debe funcionar igual que el query (antes se ignoraba en silencio)."""
    from src.api.server import _session_log

    sid = client.post("/api/sessions", json={"project": "pytest-branch"}).json()["id"]
    child = None
    try:
        client.post(f"/api/sessions/{sid}/messages",
                    json={"role": "user", "content": "mensaje heredado 5521"})
        r = client.post(f"/api/sessions/{sid}/branch", json={"copy_messages": True})
        assert r.status_code == 200 and r.json()["ok"] is True
        child = r.json()["session"]["id"]
        contents = [m.content for m in _session_log().get_messages(child)]
        assert any("5521" in (c or "") for c in contents), contents
        # el query sigue funcionando
        r2 = client.post(f"/api/sessions/{sid}/branch?copy_messages=true")
        assert r2.status_code == 200
        client.delete(f"/api/sessions/{r2.json()['session']['id']}")
    finally:
        if child:
            client.delete(f"/api/sessions/{child}")
        client.delete(f"/api/sessions/{sid}")


def test_ws_chat_mismo_contrato_json_en_exito_y_error(monkeypatch):
    """Éxito y error devuelven JSON (antes: texto crudo vs JSON, imposible de distinguir)."""
    import types

    from src.api import server as srv

    before = {s.id for s in srv._session_log().list_sessions(project="ws", limit=1000)}

    async def _fake_run(message, session_id=None, **kw):
        return types.SimpleNamespace(final_output=f"eco:{message}", success=True)

    monkeypatch.setattr(srv, "_run_agent_once", _fake_run)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_text('{"message": "hola"}')
        ok = ws.receive_json()
        assert ok["success"] is True
        assert ok["reply"] == "eco:hola"
        assert ok["session_id"]

    async def _boom(*a, **kw):
        raise RuntimeError("proveedor caído")

    monkeypatch.setattr(srv, "_run_agent_once", _boom)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_text("hola")
        bad = ws.receive_json()
        assert bad["success"] is False
        assert "RuntimeError" in bad["error"]
        assert bad["reply"] == ""
    # No dejar sesiones de prueba en la base real del usuario.
    for s in srv._session_log().list_sessions(project="ws", limit=1000):
        if s.id not in before:
            client.delete(f"/api/sessions/{s.id}")


def test_get_endpoints_programaticos_sin_ui():
    """Endpoints solo-API (RAG/learning/skill-opt/runtime/grafo): deben responder 200.

    No tienen UI; si se rompen, nadie se entera. Este test los mantiene vivos
    y documenta que son API programática (ver docs/CAPABILITY_MATRIX.md).
    """
    for path in [
        "/api/cloud/free-models",
        "/api/rag/index-status",
        "/api/rag/stats",
        "/api/learning/stats",
        "/api/learning/recommend",
        "/api/skill-opt/stats",
        "/api/skill-opt/heldout",
        "/api/runtime",
        "/api/graph/status",
    ]:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
        assert isinstance(r.json(), dict), path


def test_status_reporta_aprobaciones_pendientes():
    """/api/status alimenta la campana de la UI: debe incluir el conteo de aprobaciones."""
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "approvals_pending" in body
    assert isinstance(body["approvals_pending"], int) and body["approvals_pending"] >= 0
