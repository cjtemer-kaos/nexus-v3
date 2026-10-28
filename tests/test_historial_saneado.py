"""Saneado del historial: pares assistant(tool_calls)/tool completos en el wire."""

from __future__ import annotations

from src.core.agent_loop import _SIN_RESULTADO, AgentLoop
from src.core.provider_base import LLMMessage, ToolCall


def _loop():
    loop = object.__new__(AgentLoop)
    loop.tools = None
    return loop


def _tc(i: str, name: str = "exec") -> ToolCall:
    return ToolCall(id=i, name=name, arguments={"command": "dir"})


def test_par_completo_no_se_toca():
    msgs = [
        LLMMessage(role="user", content="hola"),
        LLMMessage(role="assistant", content="", tool_calls=(_tc("a"),)),
        LLMMessage(role="tool", content="ok", tool_call_id="a"),
    ]
    out = _loop()._sanear_historial(msgs)
    assert [(m.role, getattr(m, "tool_call_id", "")) for m in out] == [
        ("user", ""), ("assistant", ""), ("tool", "a")]


def test_hueco_se_cierra_con_resultado_sintetico():
    msgs = [
        LLMMessage(role="assistant", content="", tool_calls=(_tc("a"), _tc("b"))),
        LLMMessage(role="tool", content="ok", tool_call_id="a"),
    ]
    out = _loop()._sanear_historial(msgs)
    assert [m.role for m in out] == ["assistant", "tool", "tool"]
    assert out[2].content == _SIN_RESULTADO
    assert out[2].tool_call_id == "b"


def test_tool_huerfano_se_degrada_a_nota():
    msgs = [
        LLMMessage(role="assistant", content="hola"),
        LLMMessage(role="tool", content="resultado viejo", tool_call_id="x"),
    ]
    out = _loop()._sanear_historial(msgs)
    assert [m.role for m in out] == ["assistant", "assistant"]
    assert "resultado viejo" in out[1].content


def test_hueco_antes_de_mensaje_siguiente():
    msgs = [
        LLMMessage(role="assistant", content="", tool_calls=(_tc("a"),)),
        LLMMessage(role="user", content="segui"),
    ]
    out = _loop()._sanear_historial(msgs)
    assert [m.role for m in out] == ["assistant", "tool", "user"]


def test_vacio_no_explota():
    assert _loop()._sanear_historial([]) == []
