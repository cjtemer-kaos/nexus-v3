import asyncio

import pytest

import src.core.approvals as approvals_module
from src.core.approval_policy import ApprovalContext, check_tool_approval
from src.core.approvals import Approvals
from src.core.tool_registry import ToolRegistry


@pytest.fixture()
def appr(tmp_path, monkeypatch):
    a = Approvals(tmp_path / "approvals.db")
    monkeypatch.setattr(approvals_module, "_APPROVALS", a)
    return a


def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    def exec_tool(command: str) -> str:
        return "ran:" + command

    def echo(text: str) -> str:
        return text

    def mcp_call(server: str, tool: str, arguments: dict | None = None) -> str:
        return f"mcp:{server}:{tool}"

    def apply_patch(patch: str) -> str:
        return "patched:" + patch

    def write_file(path: str, content: str = "") -> str:
        return "wrote:" + path

    def learn(content: str) -> str:
        return "learned"

    reg.register_function(exec_tool, name="exec", description="exec", category="local",
                          parameters={"type": "object",
                                      "properties": {"command": {"type": "string"}},
                                      "required": ["command"]})
    reg.register_function(echo, name="echo", description="echo", category="local",
                          parameters={"type": "object",
                                      "properties": {"text": {"type": "string"}},
                                      "required": ["text"]})
    reg.register_function(mcp_call, name="mcp_call", description="mcp", category="mcp",
                          parameters={"type": "object",
                                      "properties": {"server": {"type": "string"},
                                                     "tool": {"type": "string"},
                                                     "arguments": {"type": "object"}},
                                      "required": ["server", "tool"]})
    reg.register_function(apply_patch, name="apply_patch", description="patch", category="local",
                          parameters={"type": "object",
                                      "properties": {"patch": {"type": "string"}},
                                      "required": ["patch"]})
    reg.register_function(write_file, name="write_file", description="write", category="local",
                          parameters={"type": "object",
                                      "properties": {"path": {"type": "string"},
                                                     "content": {"type": "string"}},
                                      "required": ["path"]})
    reg.register_function(learn, name="learn", description="learn", category="local",
                          parameters={"type": "object",
                                      "properties": {"content": {"type": "string"}},
                                      "required": ["content"]})
    return reg


def _ctx(session_id="s1", *, interactive=False, policy="assisted",
         timeout_s=1.0, auto_apply="assisted", mcp_read_only=None):
    cfg = {"approval": {"mode": policy, "timeout_s": timeout_s}}
    if mcp_read_only is not None:
        cfg["approval"]["mcp_read_only"] = mcp_read_only
    return ApprovalContext(session_id=session_id, interactive=interactive,
                           policy=policy, timeout_s=timeout_s,
                           auto_apply=auto_apply, config=cfg)


def test_non_interactive_denies_dangerous_and_allows_safe(appr):
    reg = _registry()
    ctx = _ctx()
    assert asyncio.run(reg.dispatch("echo", {"text": "hola"}, approval=ctx,
                                    session_id="s1")) == "hola"
    for cmd in ("git push --force origin main", "npm publish", "docker push x"):
        with pytest.raises(PermissionError):
            asyncio.run(reg.dispatch("exec", {"command": cmd}, approval=ctx,
                                     session_id="s1"))
    # comando normal no se gatea
    assert asyncio.run(reg.dispatch("exec", {"command": "echo ok"}, approval=ctx,
                                    session_id="s1")) == "ran:echo ok"


def test_mcp_read_only_allowlist_and_gate(appr):
    reg = _registry()
    ctx = _ctx()
    # allowlist default: brave-search/* es read-only
    assert asyncio.run(reg.dispatch("mcp_call",
                                    {"server": "brave-search", "tool": "search"},
                                    approval=ctx, session_id="s1")) == "mcp:brave-search:search"
    with pytest.raises(PermissionError):
        asyncio.run(reg.dispatch("mcp_call",
                                 {"server": "github", "tool": "create_issue"},
                                 approval=ctx, session_id="s1"))
    # allowlist custom
    ctx2 = _ctx(mcp_read_only=["github/create_issue"])
    assert asyncio.run(reg.dispatch("mcp_call",
                                    {"server": "github", "tool": "create_issue"},
                                    approval=ctx2, session_id="s1")) == "mcp:github:create_issue"


def test_mcp_always_grant_is_by_server_tool(appr):
    reg = _registry()
    ctx = _ctx("s4", interactive=True, timeout_s=3.0)

    async def approver():
        for _ in range(80):
            pend = [a for a in appr.list("s4") if a["status"] == "pending"]
            if pend:
                return appr.resolve(pend[0]["id"], True, scope="always")
            await asyncio.sleep(0.02)
        raise AssertionError("no se encolo el pedido")

    async def flow():
        t = asyncio.create_task(approver())
        r1 = await reg.dispatch("mcp_call",
                                {"server": "github", "tool": "create_issue",
                                 "arguments": {"title": "a"}},
                                approval=ctx, session_id="s4")
        await t
        # Distintos args: el grant always es por server+tool.
        r2 = await reg.dispatch("mcp_call",
                                {"server": "github", "tool": "create_issue",
                                 "arguments": {"title": "b"}},
                                approval=ctx, session_id="s4")
        return r1, r2

    assert asyncio.run(flow()) == ("mcp:github:create_issue", "mcp:github:create_issue")
    grants = appr.list_grants("s4")
    assert any(g["action"] == "mcp:github:create_issue" and g["scope"] == "always"
               and g["key_hash"] == "" for g in grants)


def test_subagent_inherits_parent_deny(appr):
    reg = _registry()
    # El padre nego `npm publish` para su sesion.
    pub_args = {"command": "npm publish"}
    import src.core.approvals as am
    aid = appr.propose("parent", "exec", pub_args,
                       action="publish",
                       key_hash=am.fingerprint("exec", "publish", pub_args))
    assert appr.resolve(aid["id"], False, scope="session")["ok"]
    # El hijo corre con policy auto (por si solo dejaria pasar) pero debe
    # heredar el deny del padre.
    child = ApprovalContext(session_id="child", parent_session_id="parent",
                            interactive=False, policy="auto", auto_apply="auto",
                            config={"approval": {"mode": "auto"}})
    with pytest.raises(PermissionError):
        asyncio.run(reg.dispatch("exec", pub_args, approval=child, session_id="child"))


def test_write_file_fails_closed_without_ui(appr, tmp_path):
    from src.api.server import _build_registry
    from src.core.scope import Scope, ScopeConfig

    scope = Scope(ScopeConfig(allowed_roots=(str(tmp_path),), permission="full"),
                  project_root=tmp_path)
    cfg = {"agent_loop": {"auto_apply": "approve"}, "scope": {"permission": "full"}}
    reg = _build_registry(scope, "s5", cfg, helpers=False)
    reg.approval = _ctx("s5", interactive=False)
    with pytest.raises(PermissionError):
        asyncio.run(reg.dispatch("write_file", {"path": "a.txt", "content": "x"},
                                 session_id="s5"))
    assert not (tmp_path / "a.txt").exists()
    # policy=never tambien es fail-closed aunque el contexto diga interactive.
    reg.approval = _ctx("s5", interactive=True, policy="never")
    with pytest.raises(PermissionError):
        asyncio.run(reg.dispatch("write_file", {"path": "b.txt", "content": "x"},
                                 session_id="s5"))
    assert not (tmp_path / "b.txt").exists()


def test_apply_patch_gated_when_auto_apply_approve(appr):
    reg = _registry()
    ctx = _ctx(auto_apply="approve")
    with pytest.raises(PermissionError):
        asyncio.run(reg.dispatch("apply_patch", {"patch": "x"}, approval=ctx,
                                 session_id="s1"))
    # write_file sigue a cargo del handler (no se duplica en dispatch)
    assert asyncio.run(reg.dispatch("write_file", {"path": "a.txt"},
                                    approval=ctx, session_id="s1")) == "wrote:a.txt"


def test_session_grant_is_reused(appr):
    reg = _registry()
    ctx = _ctx("s2", interactive=True, timeout_s=3.0)

    async def approver():
        for _ in range(80):
            pend = [a for a in appr.list("s2") if a["status"] == "pending"]
            if pend:
                return appr.resolve(pend[0]["id"], True, scope="session")
            await asyncio.sleep(0.02)
        raise AssertionError("no se encolo el pedido")

    async def flow():
        t = asyncio.create_task(approver())
        r1 = await reg.dispatch("exec", {"command": "npm publish"}, approval=ctx,
                                session_id="s2")
        await t
        r2 = await reg.dispatch("exec", {"command": "npm publish"}, approval=ctx,
                                session_id="s2")
        return r1, r2

    r1, r2 = asyncio.run(flow())
    assert r1 == r2 == "ran:npm publish"
    grants = appr.list_grants("s2")
    assert any(g["decision"] == "allow" and g["scope"] == "session" for g in grants)


def test_once_grant_is_consumed(appr):
    reg = _registry()
    ctx = _ctx("s3", interactive=True, timeout_s=1.0)

    async def approver():
        for _ in range(80):
            pend = [a for a in appr.list("s3") if a["status"] == "pending"]
            if pend:
                return appr.resolve(pend[0]["id"], True, scope="once")
            await asyncio.sleep(0.02)
        raise AssertionError("no se encolo el pedido")

    async def flow():
        t = asyncio.create_task(approver())
        r1 = await reg.dispatch("exec", {"command": "npm publish"}, approval=ctx,
                                session_id="s3")
        await t
        # Sin nuevo aprobador, el segundo pedido expira (fail-closed).
        with pytest.raises(PermissionError):
            await reg.dispatch("exec", {"command": "npm publish"}, approval=ctx,
                               session_id="s3")
        return r1

    assert asyncio.run(flow()) == "ran:npm publish"
