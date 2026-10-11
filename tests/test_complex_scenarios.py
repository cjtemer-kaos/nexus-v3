"""Pruebas complejas — escenarios multi-paso del harness (deterministas).

Ejercitan el AgentLoop real con tool calls en secuencia (write→read→exec),
aislamiento por ayudante, compactación con offload, seguridad y event sourcing.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from src.core.agent_loop import AgentLoop
from src.core.agent_registry import AgentRegistry
from src.core.compaction import CompactionPipeline
from src.core.event_store import EventStore
from src.core.hooks_engine import HookAction, HookResult, HooksEngine
from src.core.provider_base import BaseProvider, LLMResponse, ToolCall
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.scope import Scope, ScopeConfig, ScopeDenied
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry


class ScriptedProvider(BaseProvider):
    """Devuelve una secuencia de respuestas (tool_calls o final) por turno."""
    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.i = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        step = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if step.get("tool"):
            tc = ToolCall(id=f"c{self.i}", name=step["tool"], arguments=step.get("args", {}))
            return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="scripted")
        return LLMResponse(content=step.get("final", "ok"), model="scripted", usage={"total_tokens": 5})

    async def health(self):
        return True

    async def list_models(self):
        return ["scripted"]


def _router(prov: BaseProvider) -> ProviderRouter:
    r = ProviderRouter()
    r.register_provider("mock", prov)
    r.register_model(ModelRoute(id="scripted", provider_name="mock", tier=3))
    return r


def test_brain_ignores_ambient_nexus_brain(monkeypatch):
    """nexus-v3 NO debe enlazar el brain de otro proyecto via NEXUS_BRAIN."""
    monkeypatch.setenv("NEXUS_BRAIN", r"D:\otro-proyecto\brain")
    monkeypatch.delenv("NEXUS_V3_BRAIN", raising=False)
    from src.brain.link import brain_path, DEFAULT_BRAIN
    assert brain_path().resolve() == DEFAULT_BRAIN.resolve()
    assert "otro-proyecto" not in str(brain_path())


def test_multistep_write_read_exec():
    """Un turno compuesto: escribe archivo, lo lee, ejecuta y responde."""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            slog = SessionLog(":memory:")
            es = EventStore(":memory:")
            scope = Scope(ScopeConfig(allowed_roots=(str(root),), deny_patterns=()), project_root=root)
            reg = ToolRegistry()
            reg.register_function(
                lambda path, content: (root / path).write_text(content, encoding="utf-8") or f"escrito {path}",
                name="write_file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
            )
            reg.register_function(
                lambda path: (root / path).read_text(encoding="utf-8"),
                name="read_file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            )
            reg.register_function(
                lambda command: "salida-exec-ok",
                name="exec",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
            )
            prov = ScriptedProvider([
                {"tool": "write_file", "args": {"path": "nota.txt", "content": "contenido-prueba"}},
                {"tool": "read_file", "args": {"path": "nota.txt"}},
                {"tool": "exec", "args": {"command": "echo hola"}},
                {"final": "listo: contenido-prueba"},
            ])
            loop = AgentLoop(provider_router=_router(prov), tool_registry=reg, scope=scope,
                             session_log=slog, event_store=es)
            res = await loop.run(task="crea, lee y ejecuta", session_id=slog.create_session().id, hint="coding")
            assert res.success and "contenido-prueba" in res.final_output
            assert (root / "nota.txt").read_text(encoding="utf-8") == "contenido-prueba"
            used = [s.tool_used for s in res.steps if s.tool_used and s.phase == "act"]
            assert used == ["write_file", "read_file", "exec"]
            assert es.count() >= 3
            assert res.trajectory is not None and len(res.trajectory.steps) >= 4
            slog.close(); es.close()
    asyncio.run(run())


def test_agent_workspace_isolation():
    """El scope del ayudante permite su workspace y deniega el resto del proyecto."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        reg = AgentRegistry(base / "data" / "agents" / "registry.json")
        a = reg.create(name="iso")
        ws = Path(reg.resolve("iso")["workspace_abs"])
        assert ws.is_dir()
        scope = Scope(ScopeConfig(allowed_roots=(str(ws),), deny_patterns=()), project_root=base)
        scope.check_path(str(ws / "f.txt"))  # dentro: ok
        with pytest.raises(ScopeDenied):
            scope.check_path(str(base / "otro.txt"))  # fuera: denegado


def test_compaction_large_transcript_offloads():
    """Transcript enorme → compacta y persiste el contenido grande en spill."""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            pipe = CompactionPipeline({"snip": {"keep_recent": 3},
                                       "budget": {"max_tool_result_chars": 80, "persist_dir": td}})
            msgs = [{"role": "system", "content": "sys"}]
            for i in range(60):
                msgs.append({"role": "user", "content": f"turno {i}"})
                msgs.append({"role": "tool", "content": "Z" * 2000})
            out, metrics = await pipe.compact(msgs, limit=600)
            assert metrics.tokens_saved > 0 and metrics.compressed_tokens < metrics.original_tokens
            assert any(m.get("role") == "system" for m in out)
            assert len(out) < len(msgs)
            assert list(Path(td).glob("spill_*.txt"))
    asyncio.run(run())


def test_security_hook_blocks_tool():
    """Un hook STOP en pre_tool_use bloquea el dispatch (gate en código, no prompt)."""
    async def run():
        reg = ToolRegistry()
        reg.register_function(lambda text: f"eco:{text}", name="echo",
                              parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
        hooks = HooksEngine({"security_gate": True})
        hooks.on("pre_tool_use", lambda ph, pl: HookResult(action=HookAction.STOP, message="bloqueado por politica"))
        with pytest.raises(PermissionError):
            await reg.dispatch("echo", {"text": "x"}, hooks=hooks)
    asyncio.run(run())


def test_blocked_commands():
    from src.api.server import _blocked_command
    for cmd in ["rm -rf /", "del /s /q C:\\", "mkfs.ext4 /dev/sda", "rd /s /q C:\\Windows", "shutdown /s"]:
        assert _blocked_command(cmd) is not None, cmd
    assert _blocked_command("echo hola") is None


def test_delegation_tools_registered_and_gema_list():
    """El chat expone las tools de delegación y gema_list devuelve el catálogo."""
    import json
    from src.api.server import _build_registry, _build_scope, _get_router, _load_config_safe
    cfg = _load_config_safe()
    scope = _build_scope(cfg)
    reg = _build_registry(scope, "t", cfg, router=_get_router(cfg))
    names = {t.name for t in reg.list()}
    assert {"gema_list", "agent_list", "gema_run", "agent_run"} <= names
    out = json.loads(reg.get("gema_list").handler())
    assert len(out["gemas"]) >= 20
    assert all("assigned_model" in g for g in out["gemas"])
