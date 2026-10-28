"""El historial que se manda al modelo no puede tener tool_calls sin respuesta.

Caso real (sesión de eMule, 2026-10-28): el loop agregaba el assistant con N
tool_calls y después las ejecutaba una por una; cuando un guard cortaba el turno
en la mitad, las que faltaban quedaban sin resultado. En cada request siguiente
el saneador las rellenaba con "[sin resultado]" (6–12 huecos por request) y el
modelo perdía lo que sí había hecho: volvía a leer los mismos archivos.
"""
from __future__ import annotations

from src.core.agent_loop import AgentLoop
from src.core.provider_base import LLMMessage, ToolCall
from src.core.session_log import SessionLog


def _loop(slog: SessionLog) -> AgentLoop:
    return AgentLoop(provider_router=None, tool_registry=None, session_log=slog)


def test_cierra_los_tool_calls_que_quedaron_sin_resultado():
    slog = SessionLog(":memory:")
    try:
        sess = slog.create_session(project="tool-calls-pendientes")
        loop = _loop(slog)
        msgs = [
            LLMMessage(role="user", content="hacé tres cosas"),
            LLMMessage(role="assistant", content="", tool_calls=(
                ToolCall(id="t1", name="read_file", arguments={"path": "a.py"}),
                ToolCall(id="t2", name="read_file", arguments={"path": "b.py"}),
                ToolCall(id="t3", name="read_file", arguments={"path": "c.py"}),
            )),
            LLMMessage(role="tool", content="contenido de a.py", tool_call_id="t1"),
        ]

        cerrados = loop._cerrar_tool_calls_pendientes(msgs, sess.id)

        assert cerrados == 2
        tools = [m for m in msgs if m.role == "tool"]
        assert [m.tool_call_id for m in tools] == ["t1", "t2", "t3"]
        assert all("no ejecutada" in (m.content or "") for m in tools[1:])
        # Y quedan persistidos: si no, el próximo replay vuelve a tener el hueco.
        persistidos = [m for m in slog.get_messages(sess.id) if m.role == "tool"]
        assert len(persistidos) == 2
    finally:
        slog.close()


def test_no_toca_nada_si_el_par_esta_completo():
    slog = SessionLog(":memory:")
    try:
        sess = slog.create_session(project="tool-calls-completos")
        loop = _loop(slog)
        msgs = [
            LLMMessage(role="assistant", content="", tool_calls=(
                ToolCall(id="x1", name="exec", arguments={"cmd": "dir"}),
            )),
            LLMMessage(role="tool", content="salida", tool_call_id="x1"),
        ]

        assert loop._cerrar_tool_calls_pendientes(msgs, sess.id) == 0
        assert len(msgs) == 2
        assert slog.get_messages(sess.id) == []
    finally:
        slog.close()
