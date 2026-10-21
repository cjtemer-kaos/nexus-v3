"""Cobertura P1/P2 del informe HARNESS-REUSE-2026-10-20.

Compactacion con presupuesto/reuso/cooldown, poda de payload, guardrail de
secuencia, sandbox ladder, repo-map y PTY con fallback.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.core.agent_loop import AgentLoop
from src.core.compaction import CompactionPipeline
from src.core.provider_base import LLMMessage, LLMResponse
from src.core.scope import Scope, ScopeConfig, ScopeDenied, normalize_permission
from src.core.sequence_guard import SequenceGuard


class _Provider:
    def __init__(self, content: str = "<summary>resumen</summary>", fail: bool = False):
        self.content = content
        self.fail = fail
        self.calls = 0

    async def chat(self, messages, model=None, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("llm caido")
        return LLMResponse(content=self.content)


class _Routed:
    def __init__(self, provider, model="fake"):
        self.provider = provider
        self.model = model


class _Router:
    def __init__(self, provider):
        self.provider = provider
        self.resolves = 0

    def resolve(self, task_hint="simple"):
        self.resolves += 1
        return _Routed(self.provider)


def _big_messages(n: int = 20, size: int = 5000) -> list[dict]:
    out = []
    for i in range(n):
        role = "assistant" if i % 2 else "user"
        out.append({"role": role, "content": f"msg-{i} " + ("x" * size)})
    return out


def test_summary_budget_is_dynamic_with_floor_and_ceiling():
    pipe = CompactionPipeline({
        "summary": {"min_tokens": 2000, "ratio": 0.20, "max_tokens": 12000},
    })
    assert pipe.summary_budget(32000) == 6400
    assert pipe.summary_budget(4000) == 2000       # piso
    assert pipe.summary_budget(200000) == 12000    # techo


def test_summary_reuses_previous_when_little_progress():
    async def run():
        pipe = CompactionPipeline({
            "snip": {"keep_recent": 2},
            "budget": {"max_tool_result_chars": 2000, "persist_dir": ""},
            "summary": {"min_tokens": 200, "ratio": 0.2, "max_tokens": 400,
                        "minimum_progress_messages": 2},
        })
        provider = _Provider()
        router = _Router(provider)
        out, metrics = await pipe.summarize_with_llm(
            _big_messages(), limit=200, router=router,
            previous_summary="[resumen de 18 mensajes anteriores] VIEJO",
            progress_messages=1,
        )
        assert provider.calls == 0, "no debe gastar LLM con poco progreso"
        joined = "\n".join(str(m.get("content") or "") for m in out)
        assert "VIEJO" in joined
        assert "reutilizado" in joined
        assert "summary_llm" in metrics.layers_applied or "summary" in metrics.layers_applied

    asyncio.run(run())


def test_summary_llm_uses_budget_and_handoff():
    async def run():
        pipe = CompactionPipeline({
            "snip": {"keep_recent": 2},
            "budget": {"max_tool_result_chars": 2000, "persist_dir": ""},
            "summary": {"min_tokens": 300, "ratio": 0.2, "max_tokens": 600,
                        "minimum_progress_messages": 1,
                        "handoff_prefix": "[handoff] contexto compactado"},
        })
        provider = _Provider(content="<summary>estado actual</summary>")
        router = _Router(provider)
        out, _ = await pipe.summarize_with_llm(
            _big_messages(), limit=200, router=router,
            previous_summary="[resumen de 10 mensajes anteriores] previo",
            progress_messages=5,
        )
        assert provider.calls == 1
        joined = "\n".join(str(m.get("content") or "") for m in out)
        assert "[handoff] contexto compactado" in joined
        assert "estado actual" in joined
        assert "actualizado" in joined

    asyncio.run(run())


def test_summary_failure_sets_cooldown_without_crashing():
    async def run():
        pipe = CompactionPipeline({
            "snip": {"keep_recent": 2},
            "budget": {"max_tool_result_chars": 2000, "persist_dir": ""},
            "summary": {"min_tokens": 200, "ratio": 0.2, "max_tokens": 400,
                        "minimum_progress_messages": 1,
                        "failure_cooldown_s": 600},
        })
        bad = _Provider(fail=True)
        out, _ = await pipe.summarize_with_llm(_big_messages(), 200, _Router(bad))
        assert bad.calls == 1
        assert out
        good = _Provider()
        out2, _ = await pipe.summarize_with_llm(_big_messages(), 200, _Router(good))
        assert good.calls == 0, "cooldown debe evitar el segundo intento"

    asyncio.run(run())


def test_prune_for_llm_keeps_recent_and_does_not_mutate_history():
    pipe = CompactionPipeline({"prune": {
        "enabled": True, "keep_recent": 1, "max_tool_result_chars": 100}})
    msgs = [
        LLMMessage(role="user", content="hola"),
        LLMMessage(role="tool", content="A" * 1000, tool_call_id="1"),
        LLMMessage(role="tool", content="B" * 1000, tool_call_id="2"),
        LLMMessage(role="tool", content="C" * 1000, tool_call_id="3"),
    ]
    pruned = pipe.prune_for_llm(msgs)
    assert pruned[0].content == "hola"
    assert len(pruned[1].content) < 1000
    assert "podado" in pruned[1].content
    assert "podado" in pruned[2].content   # solo el ULTIMO queda intacto
    assert pruned[3].content == "C" * 1000 # ultimo intacto
    assert len(msgs[1].content) == 1000      # historial original intacto


def test_sequence_guard_heuristic_high_medium_low():
    async def run():
        g = SequenceGuard({"guard": {"sequence": {
            "enabled": True, "mode": "heuristic", "repeat_medium": 3}}})
        assert (await g.evaluate("exec", {"command": "rm -rf /"})).risk == "HIGH"
        g.record("exec", {"command": "echo hola"}, True)
        g.record("exec", {"command": "echo hola"}, True)
        v = await g.evaluate("exec", {"command": "echo hola"})
        assert v.risk == "MEDIUM"
        assert (await g.evaluate("read_file", {"path": "src/x.py"})).risk == "LOW"

    asyncio.run(run())


def test_sequence_guard_llm_only_on_suspicious_sequence():
    async def run():
        provider = _Provider(content=json.dumps({"risk": "HIGH", "reason": "repite fallo"}))
        router = _Router(provider)
        g = SequenceGuard({"guard": {"sequence": {
            "enabled": True, "mode": "auto", "repeat_medium": 2}}}, router=router)
        # LOW no consulta al LLM.
        low = await g.evaluate("read_file", {"path": "x"})
        assert low.risk == "LOW" and provider.calls == 0
        g.record("exec", {"command": "false"}, False)
        g.record("exec", {"command": "false"}, False)
        verdict = await g.evaluate("exec", {"command": "false"})
        assert provider.calls == 1
        assert verdict.risk == "HIGH" and verdict.source == "llm"

    asyncio.run(run())


def test_sequence_policy_formal_rules_win_over_heuristic():
    async def run():
        g = SequenceGuard({"guard": {"sequence": {
            "enabled": True, "mode": "heuristic",
            "policy": {"enabled": True, "rules": [
                {"id": "deny-echo", "action": "deny", "tools": ["exec"],
                 "pattern": "echo"},
                {"id": "allow-echo", "action": "allow", "tools": ["exec"],
                 "pattern": "echo"},
                {"id": "medium-env", "action": "medium",
                 "tools": ["read_file"], "pattern": r"\.env"},
                {"id": "allow-dir", "action": "allow", "tools": ["exec"],
                 "pattern": "^dir"},
            ]}}}})
        denied = await g.evaluate("exec", {"command": "echo hola"})
        assert denied.risk == "HIGH"
        assert denied.source == "policy:deny-echo"
        allowed = await g.evaluate("exec", {"command": "dir"})
        assert allowed.risk == "LOW"
        assert allowed.source == "policy:allow-dir"
        secret = await g.evaluate("read_file", {"path": ".env"})
        assert secret.risk == "MEDIUM"

    asyncio.run(run())


def test_sequence_policy_allow_never_overrides_catastrophic():
    g = SequenceGuard({"guard": {"sequence": {
        "enabled": True, "mode": "heuristic",
        "policy": {"enabled": True, "rules": [
            {"id": "allow-all", "action": "allow", "tools": ["*"],
             "pattern": ".*"},
        ]}}}})
    verdict = asyncio.run(g.evaluate("exec", {"command": "rm -rf /"}))
    assert verdict.risk == "HIGH"


def test_sequence_policy_loads_external_file(tmp_path: Path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "rules:\n"
        "- id: deny-push\n"
        "  action: deny\n"
        "  tools: [exec]\n"
        "  pattern: 'git\\s+push'\n", encoding="utf-8")
    g = SequenceGuard({"guard": {"sequence": {
        "enabled": True, "mode": "heuristic",
        "policy": {"enabled": True, "file": str(policy)}}}})
    verdict = asyncio.run(g.evaluate("exec", {"command": "git push origin main"}))
    assert verdict.risk == "HIGH"
    assert verdict.source == "policy:deny-push"


def test_sequence_guard_secret_path_in_args():
    g = SequenceGuard({"guard": {"sequence": {
        "enabled": True, "mode": "heuristic"}}})
    verdict = asyncio.run(g.evaluate("read_file", {"path": ".env"}))
    assert verdict.risk == "MEDIUM"


def test_sandbox_ladder_aliases_and_upward_only():
    assert normalize_permission("read-only") == "read_only"
    assert normalize_permission("workspace-write") == "workspace_write"
    assert normalize_permission("danger-full-access") == "full"
    scope = Scope(ScopeConfig(allowed_roots=(".",), permission="read-only"),
                  Path.cwd())
    assert scope.permission == "read_only"
    with __import__("pytest").raises(ScopeDenied):
        scope.escalate("workspace_write")  # sin aprobacion
    assert scope.escalate("workspace_write", approved=True) == "workspace_write"
    with __import__("pytest").raises(ScopeDenied):
        scope.escalate("read_only")  # nunca baja


def test_repo_map_respects_budget_and_ranks_imports(tmp_path: Path):
    from src.core.repo_map import build_repo_map

    (tmp_path / "a.py").write_text("from b import Base\n\ndef usa():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("class Base:\n    pass\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("def suelto():\n    pass\n", encoding="utf-8")
    res = build_repo_map(str(tmp_path), budget_tokens=100, max_files=20)
    assert res["stats"]["files_scanned"] == 3
    assert res["stats"]["used_tokens"] <= 100
    assert "b.py" in res["text"]
    assert res["stats"]["edges"] >= 1


def test_repo_map_tree_sitter_symbols_and_unicode(tmp_path: Path):
    from src.core.repo_map import _tree_sitter_available, build_repo_map

    if not _tree_sitter_available():
        pytest.skip("extra repomap no instalado")
    (tmp_path / "mod.py").write_text(
        "def café():\n    pass\n\nclass Núcleo:\n    pass\n", encoding="utf-8")
    res = build_repo_map(str(tmp_path), budget_tokens=200, max_files=10)
    assert res["stats"]["tree_sitter"] is True
    symbols = res["files"][0]["symbols"]
    assert "café" in symbols and "Núcleo" in symbols


def test_repo_map_tree_sitter_can_be_disabled(monkeypatch):
    from src.core.repo_map import _tree_sitter_available

    monkeypatch.setenv("NEXUS_REPOMAP", "regex")
    assert _tree_sitter_available() is False


def test_repo_map_tree_sitter_javascript_and_typescript(tmp_path: Path):
    from src.core.repo_map import _tree_sitter_available, build_repo_map

    if not _tree_sitter_available():
        pytest.skip("extra repomap no instalado")
    (tmp_path / "a.js").write_text(
        "export function saludar() { return 'hola'; }\n", encoding="utf-8")
    (tmp_path / "b.tsx").write_text(
        "export const Boton = () => <div />;\n", encoding="utf-8")
    res = build_repo_map(str(tmp_path), budget_tokens=300, max_files=10)
    by_path = {f["path"]: f for f in res["files"]}
    assert "saludar" in by_path["a.js"]["symbols"]
    assert "Boton" in by_path["b.tsx"]["symbols"]


def test_pty_falls_back_to_persistent_shell():
    from src.tools.pty_shell import reset_pty_session, run_command

    try:
        out, err, code, interrupted = asyncio.run(
            run_command("echo pty_fallback_ok", prefer_pty=True, timeout_s=10))
        assert not interrupted
        assert code == 0
        assert "pty_fallback_ok" in (out + err)
    finally:
        reset_pty_session()


def test_pty_session_persists_cwd_and_environment(tmp_path: Path):
    from src.tools.pty_shell import pty_available, reset_pty_session, run_command

    if not pty_available():
        pytest.skip("pywinpty no instalado")

    async def run():
        reset_pty_session()
        try:
            target = str(tmp_path)
            _, _, code, _ = await run_command(
                f'cd /d "{target}"', prefer_pty=True, timeout_s=10)
            assert code == 0
            out, _, code, _ = await run_command(
                "cd", prefer_pty=True, timeout_s=10)
            assert code == 0 and target.lower() in out.lower()

            await run_command(
                "set NEXUS_PTY_TEST=abc123", prefer_pty=True, timeout_s=10)
            out, _, _, _ = await run_command(
                "echo %NEXUS_PTY_TEST%", prefer_pty=True, timeout_s=10)
            assert "abc123" in out

            _, _, code, _ = await run_command(
                "cmd /c exit 7", prefer_pty=True, timeout_s=10)
            assert code == 7
        finally:
            reset_pty_session()

    asyncio.run(run())


def test_anti_loop_thresholds_are_configurable():
    loop = AgentLoop(config={"agent_loop": {"anti_loop": {
        "repeat_warn": [3, 5], "repeat_stop": 8,
        "failures_warn": 4, "failures_stop": 9}}})
    assert loop.anti_loop_repeat_warn == (3, 5)
    assert loop.anti_loop_repeat_stop == 8
    assert loop.anti_loop_failures_warn == 4
    assert loop.anti_loop_failures_stop == 9
