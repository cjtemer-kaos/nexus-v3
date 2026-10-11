"""Eval / regression harness — contrato profesional del arnes Nexus v3.

Cubre lo que un harness de mercado debe garantizar: compactacion con offload,
validacion de argumentos, gate de seguridad load-bearing, event sourcing y
memoria multi-turno. Es a la vez test y especificacion ejecutable.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from src.core.agent_loop import AgentLoop
from src.core.compaction import CompactionPipeline
from src.core.event_store import EventStore
from src.core.hooks_engine import HookAction, HookResult, HooksEngine
from src.core.iteration_budget import IterationBudget
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry, validate_args
from helpers import FinalAnswerProvider

ROOT = Path(__file__).resolve().parent.parent


def test_compaction_reduces_and_offloads():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            pipe = CompactionPipeline({
                "snip": {"keep_recent": 2},
                "budget": {"max_tool_result_chars": 50, "persist_dir": td},
            })
            big = "X" * 4000
            msgs = [{"role": "system", "content": "sys"}]
            for i in range(30):
                msgs.append({"role": "user", "content": f"u{i}"})
                msgs.append({"role": "tool", "content": big})
            out, metrics = await pipe.compact(msgs, limit=500)
            assert metrics.compressed_tokens < metrics.original_tokens
            assert metrics.tokens_saved > 0
            assert any(m.get("role") == "system" for m in out)
            spill = list(Path(td).glob("spill_*.txt"))
            assert spill, "el offload a ./data/spill no persistio contenido grande"
    asyncio.run(run())


def test_tool_arg_validation():
    schema = {"type": "object", "properties": {"text": {"type": "string"}},
              "required": ["text"], "additionalProperties": False}
    assert validate_args("echo", {"text": "ok"}, schema) == {"text": "ok"}
    for bad in ({}, {"text": 5}, {"text": "x", "extra": 1}):
        try:
            validate_args("echo", bad, schema)
            raise AssertionError(f"deberia rechazar {bad}")
        except ValueError:
            pass


def test_security_hook_is_load_bearing():
    async def run():
        reg = ToolRegistry()
        reg.register_function(lambda text: f"echo:{text}", name="echo",
                              parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
        hooks = HooksEngine({"security_gate": True})
        hooks.on("pre_tool_use", lambda ph, pl: HookResult(action=HookAction.STOP, message="bloqueado"))
        log = []
        try:
            await reg.dispatch("echo", {"text": "x"}, hooks=hooks, call_log=log.append)
            raise AssertionError("el hook STOP no bloqueo el dispatch")
        except PermissionError:
            pass
        assert any(r.get("outcome") == "hook_blocked" for r in log)
    asyncio.run(run())


def test_event_store_replay_roundtrip():
    es = EventStore(":memory:")
    try:
        es.append_event("user_message", {"c": "a"}, session_id="s1")
        es.append_event("tool_call", {"t": "echo"}, session_id="s1")
        es.append_event("assistant_message", {"c": "b"}, session_id="s1")
        evs = es.replay("s1")
        assert len(evs) == 3
        assert {e.kind for e in evs} == {"user_message", "tool_call", "assistant_message"}
        assert es.count("s1") == 3
    finally:
        es.close()


def test_multi_turn_memory_persists():
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="eval-mem")
            router = ProviderRouter()
            router.register_provider("mock", FinalAnswerProvider())
            router.register_model(ModelRoute(id="final", provider_name="mock"))
            for i in range(2):
                loop = AgentLoop(provider_router=router, tool_registry=ToolRegistry(),
                                 budget=IterationBudget(max_iterations=3), session_log=slog)
                await loop.run(task=f"turno {i}", session_id=sess.id, hint="simple")
            msgs = slog.get_messages(sess.id)
            assert len(msgs) >= 4
            assert sum(1 for m in msgs if m.role == "user") == 2
        finally:
            slog.close()
    asyncio.run(run())


def test_compaction_reactive_parses_limit():
    async def run():
        pipe = CompactionPipeline({"snip": {"keep_recent": 1}})
        msgs = [{"role": "user", "content": "x" * 500} for _ in range(50)]
        out = await pipe.reactive_compact(msgs, "context_limit 8192 exceeded, got 45000")
        assert len(out) < len(msgs)
    asyncio.run(run())
