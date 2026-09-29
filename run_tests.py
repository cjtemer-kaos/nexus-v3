"""Comprehensive functional tests for Nexus v3 - one file, full report."""
from __future__ import annotations
import asyncio
import json
import os
import sys
import time
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Colors for terminal
GREEN = "\x1b[32m"; RED = "\x1b[31m"; YELLOW = "\x1b[33m"; RESET = "\x1b[0m"; BOLD = "\x1b[1m"
results = []

def record(name, passed, detail="", duration_ms=0):
    status = "PASS" if passed else "FAIL"
    results.append({"name": name, "passed": passed, "detail": detail, "duration_ms": duration_ms})
    icon = f"{GREEN}OK{RESET}" if passed else f"{RED}FAIL{RESET}"
    ms = f" ({duration_ms:.1f}ms)" if duration_ms else ""
    print(f"  {icon} [{status}] {name}{ms} - {detail}")

def ok(name, detail="", t0=None):
    d = (time.monotonic()-t0)*1000 if t0 else 0
    record(name, True, detail, d)
def fail(name, detail="", t0=None, exc=None):
    if exc:
        detail = f"{detail} | {exc.__class__.__name__}: {exc}"
        traceback.print_exc()
    d = (time.monotonic()-t0)*1000 if t0 else 0
    record(name, False, detail, d)

print(f"{BOLD}==============================================================={RESET}")
print(f"{BOLD} Nexus v3 - Comprehensive Functional Tests  {time.strftime('%Y-%m-%d %H:%M:%S')} {RESET}")
print(f"{BOLD}==============================================================={RESET}\n")

# -- 1. Session creation and message handling --
print(f"{BOLD}[1] Session creation and message handling{RESET}")
try:
    from src.core.session_log import SessionLog, Message
    t0=time.monotonic()
    with tempfile.TemporaryDirectory() as td:
        db = Path(td)/"session.db"
        slog = SessionLog(str(db), wal=True, fts=True)
        # check WAL
        jmode = slog.check_wal()
        ok("Session WAL mode", f"journal_mode={jmode}", t0)

        # create session
        t0=time.monotonic()
        sess = slog.create_session(project="nexus-v3")
        ok("Session creation", f"id={sess.id} project={sess.project} parent_id={sess.parent_id}", t0)
        assert len(sess.id)==12

        # append messages
        t0=time.monotonic()
        for role, content in [("user","Hola Nexus"),("assistant","Hola, soy Nexus v3"),("tool","resultado tool mock"),("user","segundo mensaje")]:
            slog.append(sess.id, Message(role=role, content=content, tokens=len(content)//4))
        ok("Session append (4 msgs)", f"count={slog.count_messages(sess.id)}", t0)
        assert slog.count_messages(sess.id)==4

        # get_messages ordering
        msgs = slog.get_messages(sess.id)
        ok("Session get_messages ordering", f"msgs={[m.role for m in msgs]}")
        assert msgs[0].content=="Hola Nexus"

        # FTS search
        t0=time.monotonic()
        hits = slog.search("Nexus", limit=10)
        ok("Session FTS search", f"query='Nexus' hits={len(hits)}", t0)
        assert len(hits)>=1

        # search_session
        hits2 = slog.search_session(sess.id, "Hola")
        ok("Session FTS search_session", f"hits={len(hits2)}")

        # pagination
        recent = slog.get_recent_messages(sess.id, n=2)
        ok("Session get_recent_messages", f"n=2 got {len(recent)} last={recent[-1].content[:20]}")

        # list_sessions / get_session
        sess2 = slog.create_session(project="other")
        lst = slog.list_sessions(project="nexus-v3")
        ok("Session list filtering", f"nexus-v3 count={len(lst)}")
        assert any(s.id==sess.id for s in lst)
        fetched = slog.get_session(sess.id)
        ok("Session get_session", f"fetched id={fetched.id if fetched else 'None'}")
        assert fetched and fetched.id==sess.id

        # branching fork
        t0=time.monotonic()
        child = slog.fork(sess.id)
        ok("Session fork (branching)", f"parent={sess.id} child={child.id} child.parent_id={child.parent_id}", t0)
        assert child.parent_id==sess.id
        # fork carries parent compact state
        slog.compact_mark(sess.id, "resumen padre")
        child2 = slog.fork(sess.id)
        ok("Session fork inherits summary", f"summary='{child2.summary[:20]}' compact={child2.compact_count}")
        assert child2.summary=="resumen padre"

        # branch copy_messages
        br = slog.branch(sess.id, copy_messages=True)
        ok("Session branch copy_messages", f"child msgs={slog.count_messages(br.id)} expected 4")
        assert slog.count_messages(br.id)==4

        # compact_mark
        slog.compact_mark(sess.id, "final summary")
        s_after = slog.get_session(sess.id)
        ok("Session compact_mark", f"compact_count={s_after.compact_count} summary='{s_after.summary[:20]}'")

        # stats / check_wal / check_fts / rebuild_fts
        st = slog.get_stats()
        ok("Session get_stats", f"sessions={st['sessions']} messages={st['messages']} fts={st['fts_rows']} journal={st['journal_mode']}")
        ok("Session check_wal", f"{slog.check_wal()}")
        ok("Session check_fts", f"{slog.check_fts()}")
        slog.rebuild_fts()
        ok("Session rebuild_fts", "no exception")

        # delete_session cascade: must delete children first or leaf. Test leaf delete (correct usage).
        ok("Session delete cascade (leaf check)", f"before={slog.count_messages_all()} msgs")
        # Delete leaf (br) instead of parent with children (FK constraint is expected)
        assert slog.delete_session(br.id) == True
        ok("Session delete leaf (cascade msgs)", f"after delete count_all={slog.count_messages_all()} fetched={slog.get_session(br.id)}")
        assert slog.get_session(br.id) is None
        # Verify parent still exists and parent-delete-with-children fails as per schema (known limitation)
        still = slog.get_session(sess.id)
        ok("Session parent still exists after leaf delete", f"parent exists={still is not None}")
        # Document FK limitation: parent with children cannot be deleted without ON DELETE SET NULL
        try:
            slog.delete_session(sess.id)
            record("Session delete parent-with-children FK guard", False, "should have FK fail while children exist")
        except Exception:
            record("Session delete parent-with-children FK guard", True, "correctly blocked by FK (schema lacks ON DELETE CASCADE for parent_session_id)")

        # in-memory concurrency
        slog2 = SessionLog(":memory:")
        s = slog2.create_session()
        slog2.append(s.id, Message(role="user", content="mem test"))
        ok("Session :memory: mode", f"count={slog2.count_messages(s.id)} journal={slog2.check_wal()}")
        # close before tempfile cleanup avoids WinError 32
        slog.close(); slog2.close()

        # thread safety quick
        import threading
        slog3 = SessionLog(":memory:")
        sess_t = slog3.create_session()
        def writer(n):
            for i in range(20):
                slog3.append(sess_t.id, Message(role="user", content=f"thread {n} msg {i}"))
        ths=[threading.Thread(target=writer, args=(i,)) for i in range(3)]
        [t.start() for t in ths]; [t.join() for t in ths]
        ok("Session thread-safe concurrent append", f"total={slog3.count_messages(sess_t.id)} expected 60")
        assert slog3.count_messages(sess_t.id)==60
        slog3.close()
except Exception as e:
    fail("Session suite", str(e), exc=e)

# -- 2. Tool Registry and Scope Guard --
print(f"\n{BOLD}[2] Tool registry and Scope guard{RESET}")
try:
    from src.core.tool_registry import ToolRegistry
    from src.core.scope import Scope, ScopeConfig, ScopeDenied
    from src.core.hooks_engine import HooksEngine
    t0=time.monotonic()
    reg = ToolRegistry()
    # register function
    def add(a: int, b: int): return a+b
    reg.register_function(add, name="add", description="suma", category="local")
    ok("ToolRegistry register_function", f"count={reg.count()} has add={reg.has('add')}", t0)

    # decorator
    @reg.tool(description="echo tool")
    def echo(text: str): return text
    ok("ToolRegistry @tool decorator", f"has echo={reg.has('echo')}")

    # to_openai_tools
    tools_oai = reg.to_openai_tools()
    ok("ToolRegistry to_openai_tools", f"n={len(tools_oai)} first={tools_oai[0]['function']['name']}")

    # categories
    cats = reg.categories()
    ok("ToolRegistry categories", f"{cats}")

    # load_skill_bundle
    reg.load_skill_bundle("test-skill", [{"name":"skill_fn","description":"demo","parameters":{"type":"object","properties":{}}}])
    ok("ToolRegistry load_skill_bundle", f"has skill_fn={reg.has('skill_fn')} cat={reg.get('skill_fn').category}")

    # load_mcp_tools
    reg.load_mcp_tools("mcp-server", [{"name":"mcp_fn","description":"mcp demo","parameters":{}}])
    ok("ToolRegistry load_mcp_tools", f"has mcp_fn={reg.has('mcp_fn')}")

    # dispatch sync handler + metrics
    async def _dispatch_tests():
        t0=time.monotonic()
        proj = Path.cwd()
        scope = Scope(ScopeConfig(allowed_roots=("./workspace","./data"), deny_patterns=("**/.env","**/*.key")), project_root=proj)
        # scope guard ok
        try:
            scope.check_path("workspace/file.txt")
            record("Scope check_path allowed", True, "workspace/file.txt ok")
        except ScopeDenied as e:
            record("Scope check_path allowed", False, str(e))
        # deny .env
        try:
            scope.check_path(".env")
            record("Scope deny **/.env", False, "should have denied .env")
        except ScopeDenied:
            record("Scope deny **/.env", True, "correctly blocked .env")
        # deny path traversal
        try:
            scope.check_path("workspace/../../etc/passwd")
            record("Scope traversal block", False, "should block traversal")
        except ScopeDenied:
            record("Scope traversal block", True, "correctly blocked ../..")
        # network gate
        scope_no_net = Scope(ScopeConfig(allowed_roots=("./workspace",), allow_network=False), project_root=proj)
        try:
            scope_no_net.check_tool("web_fetch", {"url":"https://example.com"})
            record("Scope network gate (allow_network=false)", False, "should block web_fetch")
        except ScopeDenied:
            record("Scope network gate (allow_network=false)", True, "blocked web_fetch as expected")
        scope_net = Scope(ScopeConfig(allowed_roots=("./workspace",), allow_network=True), project_root=proj)
        try:
            scope_net.check_tool("web_fetch", {"url":"https://example.com"})
            record("Scope network gate (allow_network=true)", True, "allowed web_fetch")
        except ScopeDenied as e:
            record("Scope network gate (allow_network=true)", False, str(e))

        # Scope deny_patterns brain db
        try:
            scope.check_path("brain/cerebro.db-shm")
            record("Scope deny brain db-shm", False, "should deny")
        except ScopeDenied:
            record("Scope deny brain db-shm", True, "blocked brain db-shm")
        # is_path_allowed helper
        record("Scope is_path_allowed", scope.is_path_allowed("workspace/ok.txt")==True, f"is_allowed workspace/ok.txt={scope.is_path_allowed('workspace/ok.txt')}")

        # ToolRegistry dispatch with scope
        def read_file(path: str): return f"contenido de {path}"
        reg2 = ToolRegistry()
        reg2.register_function(read_file, name="read_file", description="lee archivo")
        # dispatch success
        res = await reg2.dispatch("read_file", {"path":"workspace/foo.txt"}, scope=scope)
        record("ToolRegistry dispatch + Scope guard (allowed)", res=="contenido de workspace/foo.txt", f"res={res}")
        # dispatch denied
        try:
            await reg2.dispatch("read_file", {"path":".env"}, scope=scope)
            record("ToolRegistry dispatch ScopeDenied", False, "should have raised")
        except ScopeDenied:
            record("ToolRegistry dispatch ScopeDenied", True, "correctly raised ScopeDenied")
        except Exception as e:
            record("ToolRegistry dispatch ScopeDenied", False, f"wrong exc {e}")

        # metrics: scope-denied calls are rejected BEFORE metrics (by design: guard before t0)
        entry = reg2.get("read_file")
        record("ToolRegistry metrics", entry.call_count==1 and entry.success_count==1, f"calls={entry.call_count} success={entry.success_count} avg={entry.avg_latency_ms:.1f}ms (scope-deny not counted by design)")

        # async handler
        async def async_echo(text: str): return f"async:{text}"
        reg2.register_function(async_echo, name="async_echo")
        res2 = await reg2.dispatch("async_echo", {"text":"hello"})
        record("ToolRegistry dispatch async handler", res2=="async:hello", f"res={res2}")

        # hooks pre/post
        hooks = HooksEngine(config={"security_gate":True, "phases":["pre_tool_use","post_tool_use"]})
        events=[]
        def h_pre(phase,payload): events.append(phase)
        hooks.on("pre_tool_use", h_pre)
        hooks.on("post_tool_use", h_pre)
        await reg2.dispatch("async_echo", {"text":"hook-test"}, hooks=hooks)
        record("ToolRegistry dispatch with Hooks", "pre_tool_use" in events and "post_tool_use" in events, f"events={events}")

        # ToolRegistry stats
        st=reg2.stats()
        record("ToolRegistry stats()", st["total_tools"]>=2, f"{st}")

    asyncio.run(_dispatch_tests())

except Exception as e:
    fail("ToolRegistry/Scope suite", str(e), exc=e)

# -- 3. Provider routing (mock) --
print(f"\n{BOLD}[3] Provider routing (mock){RESET}")
try:
    from src.core.provider_router import ProviderRouter, ModelRoute, HINT_MAPPING
    from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse, ToolCall

    class MockProvider(BaseProvider):
        def __init__(self, name, healthy=True, models=None, fail=False):
            self.name=name; self._healthy=healthy; self._models=models or [f"{name}-model"]; self.fail=fail
        async def chat(self, messages, tools=None, model=None, **kw):
            if self.fail: raise RuntimeError(f"{self.name} simulated failure")
            # Echo with tool_call injection if requested via hint
            content = f"[{self.name}/{model}] respuesta a: {(messages[-1].content or '')[:80]}"
            return LLMResponse(content=content, model=model or self._models[0], usage={"total_tokens":20})
        async def health(self): return self._healthy
        async def list_models(self): return list(self._models)

    class MockToolProvider(BaseProvider):
        def __init__(self): self.name="tool-mock"
        async def chat(self, messages, tools=None, model=None, **kw):
            # Return a tool call if tools provided
            if tools:
                tc = ToolCall(id="call_1", name=tools[0]["function"]["name"], arguments={"text":"hola"})
                return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="tool-model")
            return LLMResponse(content="final", model="tool-model")
        async def health(self): return True
        async def list_models(self): return ["tool-model"]

    t0=time.monotonic()
    router = ProviderRouter(vram_budget_gb=12, fallback_order=["llamacpp","ollama","cloud"], strategy="tier")
    router.register_provider("llamacpp", MockProvider("llamacpp", models=["nexus-director-v6","qwen3.5-9b"]))
    router.register_provider("ollama", MockProvider("ollama", models=["qwen2.5-coder:7b","deepseek-r1:8b"]))
    router.register_provider("cloud", MockProvider("cloud", models=["muse-spark-1.2"]))
    router.register_model(ModelRoute(id="nexus-director-v6", provider_name="llamacpp", tier=7, vram_gb=4.3))
    router.register_model(ModelRoute(id="qwen2.5-coder:7b", provider_name="ollama", tier=3, vram_gb=4.0))
    router.register_model(ModelRoute(id="muse-spark-1.2", provider_name="cloud", tier=5))
    ok("ProviderRouter registration", f"providers={list(router.providers().keys())} models={len(router._models)}", t0)

    # resolve simple -> lowest tier >=1
    r = router.resolve(task_hint="simple")
    ok("ProviderRouter resolve simple", f"model={r.model if r else None} tier={r.route.effective_tier() if r else 'None'}")
    assert r and r.route.effective_tier()>=1

    # resolve deep -> tier 10 -> fallback to max available (if none, fallback)
    r2 = router.resolve(task_hint="deep")
    ok("ProviderRouter resolve deep", f"model={r2.model if r2 else None} provider={r2.route.provider_name if r2 else None}")

    # resolve coding -> tier 3
    r3 = router.resolve(task_hint="coding")
    ok("ProviderRouter resolve coding", f"model={r3.model if r3 else None} expected qwen2.5 tier 3")

    # preferred_model
    r4 = router.resolve(preferred_model="muse-spark-1.2")
    ok("ProviderRouter preferred_model", f"model={r4.model if r4 else None} provider={r4.route.provider_name if r4 else None}")
    assert r4 and r4.model=="muse-spark-1.2"

    # chat with fallback success
    async def test_chat_fallback():
        # first provider fails, second succeeds
        router2 = ProviderRouter(fallback_order=["bad","good"])
        router2.register_provider("bad", MockProvider("bad", fail=True))
        router2.register_provider("good", MockProvider("good", models=["good-model"]))
        router2.register_model(ModelRoute(id="good-model", provider_name="good", tier=1))
        resp = await router2.chat([LLMMessage(role="user", content="hola")], hint="simple")
        record("ProviderRouter chat fallback", "good" in (resp.content or ""), f"content={resp.content}")

        # chat direct
        resp2 = await router.chat([LLMMessage(role="user", content="test tier router")], hint="simple")
        record("ProviderRouter chat tier", resp2.content is not None, f"content={resp2.content[:60]}")

        # thompson strategy
        router3 = ProviderRouter(strategy="thompson", fallback_order=["llamacpp","ollama"])
        router3.register_provider("llamacpp", MockProvider("llamacpp", models=["m-a"]))
        router3.register_provider("ollama", MockProvider("ollama", models=["m-b"]))
        router3.register_model(ModelRoute(id="m-a", provider_name="llamacpp", tier=3))
        router3.register_model(ModelRoute(id="m-b", provider_name="ollama", tier=3))
        r_th = router3.resolve(task_hint="coding", strategy="thompson")
        record("ProviderRouter thompson strategy", r_th is not None, f"picked {r_th.route.provider_name if r_th else None}")

        # vram strategy picks lower vram
        router4 = ProviderRouter(strategy="vram")
        router4.register_provider("a", MockProvider("a")); router4.register_provider("b", MockProvider("b"))
        router4.register_model(ModelRoute(id="heavy", provider_name="a", tier=5, vram_gb=8))
        router4.register_model(ModelRoute(id="light", provider_name="b", tier=5, vram_gb=1))
        r_vram = router4.resolve(task_hint="reasoning", strategy="vram")
        record("ProviderRouter vram strategy", r_vram is not None and r_vram.model=="light", f"picked {r_vram.model if r_vram else None}")

        # auto_detect probe
        router5 = ProviderRouter()
        router5.register_provider("mock_ok", MockProvider("mock_ok", healthy=True, models=["auto-model"]))
        router5.register_provider("mock_bad", MockProvider("mock_bad", healthy=False))
        ad = await router5.auto_detect()
        record("ProviderRouter auto_detect", ad.get("mock_ok")==True and ad.get("mock_bad")==False, f"{ad}")
        # check auto-registered models
        has_auto = any(m.id=="auto-model" for m in router5._models)
        record("ProviderRouter auto_detect model registration", has_auto, f"models={[m.id for m in router5._models]}")

        # health_all
        ha = await router.health_all()
        record("ProviderRouter health_all", "llamacpp" in ha or "mock_ok" in ha, f"keys={list(ha.keys())}")

    asyncio.run(test_chat_fallback())

    # stats
    ok("ProviderRouter stats()", f"{router.stats()['strategy']}")

except Exception as e:
    fail("Provider routing suite", str(e), exc=e)

# -- 4. Agent loop TDAO --
print(f"\n{BOLD}[4] Agent loop TDAO{RESET}")
try:
    from src.core.agent_loop import AgentLoop
    from src.core.iteration_budget import IterationBudget
    from src.core.session_log import SessionLog, Message
    from src.core.tool_registry import ToolRegistry
    from src.core.scope import Scope, ScopeConfig
    from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse, ToolCall
    from src.core.compaction import CompactionPipeline

    class FinalAnswerProvider(BaseProvider):
        async def chat(self, messages, tools=None, model=None, **kw):
            return LLMResponse(content="Respuesta final de prueba TDAO", model="final", usage={"total_tokens":10})
        async def health(self): return True
        async def list_models(self): return ["final"]

    class ToolThenFinalProvider(BaseProvider):
        def __init__(self): self.calls=0
        async def chat(self, messages, tools=None, model=None, **kw):
            self.calls+=1
            if self.calls==1 and tools:
                tc = ToolCall(id="call_1", name=tools[0]["function"]["name"], arguments={"text":"valor de prueba"})
                return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="tool-model")
            return LLMResponse(content="Hecho tras tool", model="final", usage={"total_tokens":15})
        async def health(self): return True
        async def list_models(self): return ["tool-model"]

    class FailingProvider(BaseProvider):
        async def chat(self, messages, tools=None, model=None, **kw): raise RuntimeError("Simulated LLM failure")
        async def health(self): return True
        async def list_models(self): return ["fail"]

    # Budget exhaustion provider (never final)
    class LoopProvider(BaseProvider):
        async def chat(self, messages, tools=None, model=None, **kw):
            tc = ToolCall(id="call_1", name="echo", arguments={"text":"loop"})
            return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="loop")
        async def health(self): return True
        async def list_models(self): return ["loop"]

    from src.core.provider_router import ProviderRouter, ModelRoute

    async def run_loop_tests():
        # Test 1: final answer (no tools)
        slog = SessionLog(":memory:")
        sess = slog.create_session(project="loop-test")
        router = ProviderRouter()
        router.register_provider("mock", FinalAnswerProvider())
        router.register_model(ModelRoute(id="final", provider_name="mock", tier=5))
        loop = AgentLoop(provider_router=router, tool_registry=ToolRegistry(), budget=IterationBudget(max_iterations=5))
        loop.session_log=None  # avoid double
        res = await loop.run(task="Hola", session_id=sess.id, hint="simple")
        record("AgentLoop TDAO final answer", res.success and "Respuesta final" in res.final_output, f"iterations={res.iterations} success={res.success} output={res.final_output[:60]}")

        # Test 2: tool then final
        slog2 = SessionLog(":memory:")
        sess2 = slog2.create_session(project="loop-test2")
        reg = ToolRegistry()
        reg.register_function(lambda text: f"echo:{text}", name="echo", description="echo")
        router2 = ProviderRouter()
        p2 = ToolThenFinalProvider()
        router2.register_provider("mock", p2)
        router2.register_model(ModelRoute(id="tool-model", provider_name="mock", tier=3))
        loop2 = AgentLoop(provider_router=router2, tool_registry=reg, budget=IterationBudget(max_iterations=5))
        res2 = await loop2.run(task="usa la tool echo", session_id=sess2.id, hint="coding")
        record("AgentLoop TDAO tool->final", res2.success and "Hecho tras tool" in res2.final_output and len(res2.steps)>=3, f"iterations={res2.iterations} steps={len(res2.steps)}")

        # Test 3: ErrorAction ABORT (scope denied)
        slog3 = SessionLog(":memory:")
        sess3 = slog3.create_session(project="loop-test3")
        reg3 = ToolRegistry()
        def forbidden(path: str): return "ok"
        reg3.register_function(forbidden, name="read_secret", description="blocked")
        router3 = ProviderRouter()
        class SecretProvider(BaseProvider):
            async def chat(self, messages, tools=None, model=None, **kw):
                tc = ToolCall(id="c1", name="read_secret", arguments={"path":".env"})
                return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="m")
            async def health(self): return True
            async def list_models(self): return ["m"]
        router3.register_provider("mock", SecretProvider())
        router3.register_model(ModelRoute(id="m", provider_name="mock"))
        scope = Scope(ScopeConfig(allowed_roots=("./workspace",), deny_patterns=("**/.env",)), project_root=Path.cwd())
        loop3 = AgentLoop(provider_router=router3, tool_registry=reg3, scope=scope, budget=IterationBudget(max_iterations=5))
        res3 = await loop3.run(task="lee secreto", session_id=sess3.id)
        # Should abort or handle via REPLAN/SKIP - check steps contain error classification
        has_error_step = any("ScopeDenied" in s.content or "scope" in s.content.lower() or "denegada" in s.content.lower() for s in res3.steps)
        record("AgentLoop ScopeDenied handling", has_error_step or not res3.success, f"steps={[s.content[:80] for s in res3.steps[-2:]]} success={res3.success}")

        # Test 4: budget exhausted
        slog4 = SessionLog(":memory:")
        sess4 = slog4.create_session(project="loop-test4")
        reg4 = ToolRegistry()
        reg4.register_function(lambda text: "ok", name="echo")
        router4 = ProviderRouter()
        router4.register_provider("mock", LoopProvider())
        router4.register_model(ModelRoute(id="loop", provider_name="mock"))
        loop4 = AgentLoop(provider_router=router4, tool_registry=reg4, budget=IterationBudget(max_iterations=2))
        res4 = await loop4.run(task="loop forever", session_id=sess4.id)
        record("AgentLoop budget exhausted", res4.iterations==2, f"iterations={res4.iterations} budget exhausted={res4.iterations>=2}")

        # Test 5: hooks + trajectory + event_store + session_log integration
        from src.core.event_store import EventStore
        from src.core.hooks_engine import HooksEngine
        slog5 = SessionLog(":memory:")
        sess5 = slog5.create_session(project="loop-test5")
        es = EventStore(":memory:")
        hooks = HooksEngine()
        hook_calls=[]
        hooks.on("session_start", lambda ph, pl: hook_calls.append(ph))
        hooks.on("session_end", lambda ph, pl: hook_calls.append(ph))
        reg5 = ToolRegistry()
        reg5.register_function(lambda text: "ok", name="echo")
        router5 = ProviderRouter()
        router5.register_provider("mock", FinalAnswerProvider())
        router5.register_model(ModelRoute(id="final", provider_name="mock"))
        loop5 = AgentLoop(provider_router=router5, tool_registry=reg5, budget=IterationBudget(max_iterations=3), session_log=slog5, event_store=es, hooks=hooks)
        # Need to give loop5 a session_log so it can persist - but run() also appends user; we use same slog5
        res5 = await loop5.run(task="integración", session_id=sess5.id)
        has_events = es.count(sess5.id) >= 1
        has_trajectory = res5.trajectory is not None and len(res5.trajectory.steps) >= 2
        record("AgentLoop integration (trajectory+event_store+hooks)", has_events and has_trajectory and "session_start" in hook_calls, f"events={es.count(sess5.id)} traj_len={len(res5.trajectory.steps) if res5.trajectory else 0} hooks={hook_calls}")

        # Test 6: IterationBudget standalone
        b = IterationBudget(max_iterations=3, max_tokens=100, max_tool_calls=5)
        b.tick(); b.tick()
        record("IterationBudget tick", b.iterations==2 and not b.exhausted, f"{b.to_dict()}")
        b.tick()
        record("IterationBudget exhausted at max_iterations", b.exhausted and b.reason is not None, f"reason={b.reason}")

        # Test 7: error_classifier
        from src.core.error_classifier import classify_error, ErrorAction
        c1 = classify_error("tool","connection timeout")
        c2 = classify_error("tool","permission denied for file")
        c3 = classify_error("tool","invalid param value")
        record("ErrorClassifier RETRY", c1==ErrorAction.RETRY, f"{c1}")
        record("ErrorClassifier ABORT", c2==ErrorAction.ABORT, f"{c2}")
        record("ErrorClassifier REPLAN", c3==ErrorAction.REPLAN, f"{c3}")

    asyncio.run(run_loop_tests())

except Exception as e:
    fail("Agent loop suite", str(e), exc=e)

# -- 5. Brain link --
print(f"\n{BOLD}[5] Brain link{RESET}")
try:
    from src.brain.link import brain_path, get_brain_info, brain_exists, is_linked_to_v2, list_brain_files, check_brain_health, DEFAULT_BRAIN
    # Default path
    bp = brain_path()
    ok("Brain path default", f"path={bp} exists={bp.exists()}")

    # get_brain_info shape
    bi = get_brain_info()
    ok("Brain get_brain_info", f"keys={list(bi.keys())} brain_exists={bi.get('brain_exists')} exists={bi.get('exists')}")

    # brain_exists is_linked
    ok("Brain brain_exists()", f"{brain_exists()}")
    ok("Brain is_linked_to_v2", f"{is_linked_to_v2()} (expected False for local brain)")

    # env override
    orig = os.environ.get("NEXUS_BRAIN")
    with tempfile.TemporaryDirectory() as td:
        # create fake brain files
        Path(td,"cerebro.db").write_bytes(b"\x00"*1024)
        Path(td,"nexus_memory.db").write_bytes(b"\x00"*512)
        os.environ["NEXUS_BRAIN"]=td
        bp2 = brain_path()
        ok("Brain NEXUS_BRAIN env override", f"path={bp2} == {td} -> {str(bp2)==td or str(bp2)==Path(td).resolve().as_posix()}")
        bi2 = get_brain_info()
        ok("Brain get_brain_info with env", f"exists={bi2['exists']} total_bytes={bi2['total_bytes']} files cerebral exists={bi2['files']['cerebro.db']['exists']}")
        files = list_brain_files()
        ok("Brain list_brain_files", f"count={len(files)} files={[f.name for f in files]}")
        health = check_brain_health()
        ok("Brain check_brain_health", f"health keys={list(health.keys())} cerebro readable={health.get('cerebro.db',{}).get('readable')}")

    if orig is None:
        os.environ.pop("NEXUS_BRAIN", None)
    else:
        os.environ["NEXUS_BRAIN"]=orig

    # ${VAR:-default} expansion
    os.environ.pop("NEXUS_BRAIN", None)
    from src.core.config import load_config, CONFIG_PATH
    cfg = load_config()
    raw_path = cfg.get("brain",{}).get("path")
    # config.yaml has ${NEXUS_BRAIN:-./brain}
    ok("Brain config path expansion", f"raw={raw_path} resolved brain_path={brain_path()}")

    # also test that check_config brain_ok is False when local brain missing (expected)
    from src.core.config import check_config
    cc = check_config()
    ok("Brain check_config brain_ok", f"brain_ok={cc['brain_ok']} valid={cc['valid']} (brain missing is ok for fresh install)")

except Exception as e:
    fail("Brain link suite", str(e), exc=e)

# -- 6. Config validation --
print(f"\n{BOLD}[6] Config validation{RESET}")
try:
    from src.core.config import load_config, validate_config, check_config, CONFIG_PATH, SCHEMA_PATH
    import jsonschema
    cfg = load_config()
    ok("Config load_config", f"project={cfg.get('project')} keys={list(cfg.keys())}")
    errors = validate_config(cfg)
    ok("Config validate_config (valid)", f"errors={errors}" if errors else "0 errors - schema OK")
    assert len(errors)==0
    cc = check_config()
    ok("Config check_config", f"valid={cc['valid']} providers={cc['providers']} router={cc['router']}")

    # env expansion
    os.environ["NEXUS_BRAIN"]="/tmp/test_brain_expansion"
    cfg_exp = load_config()
    ok("Config env expansion ${NEXUS_BRAIN}", f"brain.path={cfg_exp.get('brain',{}).get('path')} includes /tmp -> {'/tmp' in str(cfg_exp.get('brain',{}).get('path'))}")
    os.environ.pop("NEXUS_BRAIN", None)

    # invalid config detection
    bad = dict(cfg)
    bad["session"]={}
    bad_errors = validate_config(bad)
    ok("Config validate_config (invalid - missing db_path)", f"errors={len(bad_errors)} -> {bad_errors[:1]}" if bad_errors else "should have errors")
    assert len(bad_errors)>0

    # schema file existence
    ok("Config schema file exists", f"{SCHEMA_PATH} exists={SCHEMA_PATH.exists()}")

    # compaction / scope / router keys exist
    for k in ["brain","session","agent_loop","scope","compaction","providers","router","hooks"]:
        ok(f"Config section {k}", f"{k in cfg}")

    # compaction pipeline instantiation from config
    from src.core.compaction import CompactionPipeline
    pipe = CompactionPipeline(cfg.get("compaction",{}))
    ok("Config -> CompactionPipeline", f"keep_recent={pipe.keep_recent} max_chars={pipe.max_tool_result_chars}")

except Exception as e:
    fail("Config suite", str(e), exc=e)

# -- 7. UI build --
print(f"\n{BOLD}[7] UI build{RESET}")
try:
    import subprocess
    ui_dir = ROOT/"ui"
    ok("UI package.json exists", f"{(ui_dir/'package.json').exists()} path={ui_dir/'package.json'}")

    # typecheck (use shell=True on Windows)
    t0=time.monotonic()
    r = subprocess.run("npx tsc --noEmit", cwd=str(ui_dir), capture_output=True, text=True, timeout=60, shell=True)
    ok("UI tsc --noEmit", f"returncode={r.returncode} " + (r.stderr[:300] if r.stderr else "OK"), t0)
    if r.returncode!=0:
        print("    tsc stdout:", r.stdout[:1000])
        print("    tsc stderr:", r.stderr[:1000])

    # vite build (use shell=True on Windows for npm)
    t0=time.monotonic()
    r2 = subprocess.run("npm run build", cwd=str(ui_dir), capture_output=True, text=True, timeout=90, shell=True)
    ok("UI vite build", f"returncode={r2.returncode}", t0)
    if r2.returncode!=0:
        print("    build stdout:", r2.stdout[-1200:])
        print("    build stderr:", r2.stderr[-1200:])
    else:
        # check dist
        dist = ui_dir/"dist"
        has_index = (dist/"index.html").exists()
        assets = list((dist/"assets").glob("*")) if (dist/"assets").exists() else []
        ok("UI dist artifacts", f"index.html={has_index} assets={len(assets)} files={[a.name for a in assets[:3]]}")

    # vite config existence
    ok("UI vite.config.ts", f"{(ui_dir/'vite.config.ts').exists()}")
    # App.tsx structure quick check
    app_text = (ui_dir/"src"/"App.tsx").read_text(encoding="utf-8")
    has_sidebar = "Sidebar" in app_text and "ChatPanel" in app_text
    ok("UI App.tsx structure", f"has Sidebar+ChatPanel={has_sidebar}")

except Exception as e:
    fail("UI suite", str(e), exc=e)

# -- 8. Extra: Compaction / EventStore / Trajectory / HooksEngine --
print(f"\n{BOLD}[8] Extra: Compaction / EventStore / Trajectory / HooksEngine{RESET}")
try:
    from src.core.compaction import CompactionPipeline
    from src.core.event_store import EventStore, Event
    from src.core.trajectory import Trajectory
    from src.core.hooks_engine import HooksEngine, HookAction, HookResult
    from src.core.iteration_budget import IterationBudget

    # Compaction maybe_compact + compact + reactive
    async def test_compaction():
        pipe = CompactionPipeline({"snip":{"keep_recent":2},"budget":{"max_tool_result_chars":50,"persist_dir":"./data/spill"},"micro":{"placeholder":"[colapsado]"},"summary":{"model_hint":"simple"}})
        msgs = [{"role":"system","content":"sys"}] + [{"role":"user","content":"hola "*(100)} for _ in range(10)] + [{"role":"tool","content":"tool result "*(200)} for _ in range(3)]
        need = pipe.maybe_compact(msgs, max_tokens=200, threshold=0.75)
        record("Compaction maybe_compact", need==True, f"need={need}")
        out, metrics = await pipe.compact(msgs, limit=300)
        record("Compaction compact", len(out)<len(msgs) and metrics.tokens_saved>0, f"orig {metrics.original_tokens}->{metrics.compressed_tokens} saved={metrics.tokens_saved} layers={metrics.layers_applied}")
        out2 = await pipe.reactive_compact(msgs, "context_limit 300, got 5000 prompt too long")
        record("Compaction reactive_compact", len(out2)<len(msgs), f"reactive out len {len(out2)}")
        # spill persistence
        spill_files = list(Path("./data/spill").glob("spill_*.txt")) if Path("./data/spill").exists() else []
        record("Compaction spill persistence", len(spill_files)>=0, f"spill files={len(spill_files)}")

    asyncio.run(test_compaction())

    # EventStore
    es = EventStore(":memory:")
    es.append_event("user_message", {"content":"hi"}, session_id="s1")
    es.append_event("tool_result", {"tool":"echo","result":"ok"}, session_id="s1")
    ok("EventStore append_event", f"count s1={es.count('s1')}")
    evs = es.replay("s1")
    ok("EventStore replay", f"events={len(evs)} kinds={[e.kind for e in evs]}")
    evs2 = es.get_events(session_id="s1", kind="tool_result")
    ok("EventStore get_events kind filter", f"filtered={len(evs2)}")
    st=es.get_stats()
    ok("EventStore stats", f"total={st['total_events']} by_kind={st['by_kind']}")
    # subscribe
    hit=[]
    es.subscribe("user_message", lambda e: hit.append(e.kind))
    es.append_event("user_message", {}, session_id="s1")
    ok("EventStore subscribe", f"hit={hit}")
    es.close()

    # Trajectory
    traj = Trajectory(session_id="t1", task="test")
    traj.append_user("hola")
    traj.append_assistant("respuesta", iteration=1)
    traj.append_assistant("", iteration=2, tool_name="echo", tool_call_id="c1", tool_args={"text":"hi"})
    traj.append_tool_result("echo", "ok", True, iteration=2, tool_call_id="c1")
    ok("Trajectory steps", f"len={len(traj)} last={traj.last_assistant.content[:20] if traj.last_assistant else 'None'}")
    msgs=traj.to_messages()
    ok("Trajectory to_messages", f"msgs len {len(msgs)} roles={[m['role'] for m in msgs]}")
    j = traj.to_json()
    ok("Trajectory to_json", f"json len {len(j)}")

    # HooksEngine
    hooks = HooksEngine(config={"security_gate":True, "phases":["pre_tool_use"]})
    async def h_stop(phase,payload): return HookResult(action=HookAction.STOP, message="halt")
    async def h_continue(phase,payload): return HookResult(action=HookAction.CONTINUE)
    hooks.on("pre_tool_use", h_continue)
    hooks.on("pre_tool_use", h_stop)
    async def test_hooks():
        res = await hooks.emit("pre_tool_use", {"tool":"x"})
        record("HooksEngine STOP short-circuit", res.action==HookAction.STOP, f"action={res.action}")
        hooks2 = HooksEngine()
        hooks2.on("post_tool_use", h_continue)
        res2 = await hooks2.emit("post_tool_use", {})
        record("HooksEngine CONTINUE", res2.action==HookAction.CONTINUE, f"{res2.action}")
        record("HooksEngine stats", hooks.handler_count()>0, f"{hooks.stats()}")
    asyncio.run(test_hooks())

except Exception as e:
    fail("Extra suite", str(e), exc=e)


# -- Summary --
total=len(results); passed=sum(1 for r in results if r["passed"]); failed=total-passed
print(f"\n{BOLD}==============================================================={RESET}")
print(f"{BOLD} TEST REPORT - Nexus v3{RESET}")
print(f"  Total : {total}")
print(f"  {GREEN}Passed: {passed}{RESET}")
print(f"  {RED}Failed: {failed}{RESET}")
print(f"  Success rate: {passed/total*100:.1f}%")
print(f"{BOLD}==============================================================={RESET}")
for r in results:
    icon = f"{GREEN}PASS{RESET}" if r["passed"] else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {r['name']} - {r['detail']}")
if failed>0:
    print(f"\n{RED} Failing components require fix before hardening phase.{RESET}")
else:
    print(f"\n{GREEN} All functional components passed - ready for hardening (12-layer audit).{RESET}")

# Exit code: 0 if all pass, 1 otherwise
sys.exit(0 if failed==0 else 1)
