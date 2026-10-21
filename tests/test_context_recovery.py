"""Recuperacion de contexto y calibracion de tokens (ports de Cline).

Cubre:
- factor de subestimacion `actual/estimado` cap 4x, sin aflojar;
- `reactive_compact` no devuelve un request igual o mayor;
- presupuesto del request del summarizer;
- kill del arbol de procesos en `exec`.
"""
from __future__ import annotations

import asyncio
import sys
import time

import pytest

from src.core.agent_loop import (
    AgentLoop,
    get_underestimate_factor,
    record_usage_calibration,
)
from src.core.compaction import CompactionPipeline
from src.core.process_tree import run_shell_capture
from src.core.provider_base import BaseProvider, LLMResponse
from src.core.provider_router import ModelRoute, ProviderRouter


class _CaptureProvider(BaseProvider):
    def __init__(self, content: str = "<summary>estado actual</summary>"):
        self.content = content
        self.calls = 0
        self.messages = None

    async def chat(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        self.messages = list(messages)
        return LLMResponse(content=self.content)

    async def health(self):
        return True

    async def list_models(self):
        return ["fake"]


def test_underestimate_factor_uses_real_usage_and_caps_at_4x():
    record_usage_calibration("cal-a", 1000, 2000)
    assert get_underestimate_factor("cal-a") == 2.0

    record_usage_calibration("cal-b", 1000, 10000)
    assert get_underestimate_factor("cal-b") == 4.0

    # Nunca afloja: si el provider reporta MENOS que la estimacion, queda 1.0.
    record_usage_calibration("cal-c", 1000, 400)
    assert get_underestimate_factor("cal-c") == 1.0

    # Sin datos validos no toca el estado.
    record_usage_calibration("cal-d", 0, 500)
    assert get_underestimate_factor("cal-d") == 1.0


def test_effective_context_budget_shrinks_with_calibration():
    loop = AgentLoop(context_max_tokens=32000)
    record_usage_calibration("cal-loop", 1000, 2000)
    assert loop._effective_context_max_tokens("cal-loop") == 16000

    record_usage_calibration("cal-loop", 1, 10000)
    assert loop._effective_context_max_tokens("cal-loop") == 8000  # piso 1024, cap 4x

    assert loop._effective_context_max_tokens("sin-datos") == 32000


def test_reactive_compact_refuses_a_request_that_did_not_shrink():
    async def run():
        pipe = CompactionPipeline({"snip": {"keep_recent": 2}})
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hola"},
            {"role": "assistant", "content": "ok"},
        ]
        with pytest.raises(RuntimeError, match="no pudo reducir"):
            await pipe.reactive_compact(messages, "context limit 100000")

    asyncio.run(run())


def test_summarizer_input_is_budgeted():
    async def run():
        pipe = CompactionPipeline({
            "snip": {"keep_recent": 2},
            "summary": {
                "min_tokens": 200,
                "ratio": 0.2,
                "max_tokens": 600,
                "input_max_tokens": 1024,
                "minimum_progress_messages": 1,
            },
        })
        provider = _CaptureProvider()
        router = ProviderRouter()
        router.register_provider("fake", provider)
        router.register_model(ModelRoute(id="fake", provider_name="fake", tier=5))

        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "TAREA ACTUAL"}]
        for i in range(24):
            messages.append({"role": "assistant", "content": f"paso {i} " + "x" * 5000})
            messages.append({"role": "tool", "content": "resultado " + "y" * 5000,
                             "tool_call_id": f"c{i}"})
        messages.append({"role": "user", "content": "seguí"})

        out, _ = await pipe.summarize_with_llm(messages, limit=4000, router=router)
        assert provider.calls == 1
        sent = "\n".join(str(m.content or "") for m in provider.messages)
        # input_target=1024 tokens y el cap de chars del summarizer es 4096.
        assert len(sent) <= 6000, f"summarizer recibio {len(sent)} chars"

    asyncio.run(run())


def test_run_shell_capture_returns_output_and_exit_code():
    async def run():
        rc, out = await run_shell_capture("echo hola", ".", 10)
        assert rc == 0
        assert "hola" in out.lower()

    asyncio.run(run())


def test_run_shell_capture_timeout_kills_the_tree():
    async def run():
        cmd = f'"{sys.executable}" -c "import time; time.sleep(10)"'
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="arbol"):
            await run_shell_capture(cmd, ".", 0.5)
        assert time.monotonic() - started < 8

    asyncio.run(run())
