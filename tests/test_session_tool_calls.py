"""Persistencia de `tool_calls`: sin la llamada guardada, reanudar degrada el historial.

El loop guardaba el resultado de cada tool (`role=tool` + `tool_call_id`) pero
**no** la llamada del assistant que lo produjo. Al reanudar, ese `tool` quedaba
huérfano (un `tool` sin `tool_calls` previos es un 400 en providers
OpenAI-compat) y se reencuadraba como nota de assistant: se perdían los
argumentos, el id y la cadena causal.

Acá se cubre la mitad que faltaba: la columna, la migración de DBs viejas, la
reconstrucción de pares al cargar historial y la reparación de dos casos sucios
(fila vieja sin llamada, cadena cortada por un corte del proceso).
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

from src.core.agent_loop import AgentLoop, _SIN_RESULTADO, _replay_history
from src.core.iteration_budget import IterationBudget
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import Message, SessionLog
from src.core.tool_registry import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from helpers import ToolThenFinalProvider  # noqa: E402

CALLS = (
    {
        "id": "call_1",
        "type": "function",
        "function": {"name": "echo", "arguments": {"text": "hola"}},
    },
)


def _log(tmp_path: Path) -> SessionLog:
    return SessionLog(str(tmp_path / "session.db"))


# ------------------------------------------------------------------ columna


def test_roundtrip_de_tool_calls(tmp_path):
    slog = _log(tmp_path)
    try:
        sess = slog.create_session()
        slog.append(sess.id, Message(role="assistant", content="", tool_calls=CALLS))
        slog.append(sess.id, Message(role="tool", content="echo:hola",
                                     tool_name="echo", tool_call_id="call_1"))
        msgs = slog.get_messages(sess.id)
        assert [m.role for m in msgs] == ["assistant", "tool"]
        assert msgs[0].content == ""
        assert msgs[0].tool_calls == CALLS
        assert msgs[0].to_dict()["tool_calls"] == list(CALLS)
        assert msgs[1].tool_calls == ()
        assert msgs[1].tool_call_id == "call_1"
    finally:
        slog.close()


def test_migracion_de_db_sin_columna(tmp_path):
    """Una session.db anterior sigue abriendo: se agrega la columna y se lee igual."""
    from src.core.session_log import _SCHEMA

    ruta = tmp_path / "vieja.db"
    # Schema de antes de la columna: se quita la línea y la coma que la precede.
    viejo = "\n".join(
        linea for linea in _SCHEMA.splitlines() if "tool_calls" not in linea
    ).replace("display_role TEXT DEFAULT '',", "display_role TEXT DEFAULT ''")
    con = sqlite3.connect(ruta)
    try:
        con.executescript(viejo)
        con.execute(
            "INSERT INTO sessions(id, project, created_at, last_active) VALUES (?,?,?,?)",
            ("s1", "default", "2026-10-27T00:00:00", "2026-10-27T00:00:00"),
        )
        con.execute(
            "INSERT INTO messages(session_id, role, content, created_at) VALUES (?,?,?,?)",
            ("s1", "user", "hola viejo", "2026-10-27T00:00:01"),
        )
        con.commit()
    finally:
        con.close()

    slog = SessionLog(str(ruta))
    try:
        cols = {r[1] for r in slog._conn.execute("PRAGMA table_info(messages)")}
        assert "tool_calls" in cols
        msgs = slog.get_messages("s1")
        assert [m.content for m in msgs] == ["hola viejo"]
        assert msgs[0].tool_calls == ()
    finally:
        slog.close()


def test_tool_calls_ilegible_no_rompe(tmp_path):
    slog = _log(tmp_path)
    try:
        sess = slog.create_session()
        slog.append(sess.id, Message(role="assistant", content="x"))
        slog._conn.execute("UPDATE messages SET tool_calls='{no es json' WHERE session_id=?",
                           (sess.id,))
        slog._conn.commit()
        assert slog.get_messages(sess.id)[0].tool_calls == ()
    finally:
        slog.close()


def test_messages_with_ids_expone_tool_calls(tmp_path):
    """La UI de rewind/edición recibe la llamada, no solo el resultado."""
    slog = _log(tmp_path)
    try:
        sess = slog.create_session()
        slog.append(sess.id, Message(role="assistant", content="", tool_calls=CALLS))
        fila = slog.messages_with_ids(sess.id)[0]
        assert fila["tool_calls"] == list(CALLS)
    finally:
        slog.close()


# ------------------------------------------------------------ reconstruccion


def test_replay_arma_el_par_assistant_tool():
    rows = [
        {"role": "user", "content": "usa echo"},
        {"role": "assistant", "content": "", "tool_calls": list(CALLS)},
        {"role": "tool", "content": "echo:hola", "tool_name": "echo", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "listo"},
    ]
    out = _replay_history(rows)
    assert [m.role for m in out] == ["user", "assistant", "tool", "assistant"]
    assert out[1].tool_calls and out[1].tool_calls[0].id == "call_1"
    assert out[1].tool_calls[0].name == "echo"
    assert out[1].tool_calls[0].arguments == {"text": "hola"}
    assert out[2].tool_call_id == "call_1"
    # El `tool` ya no se degrada a nota cuando hay llamada que lo empareja.
    assert "[resultado" not in (out[2].content or "")


def test_replay_degrada_tool_huerfano_a_nota():
    """Filas escritas antes de persistir tool_calls: se mantiene el comportamiento viejo."""
    out = _replay_history([
        {"role": "tool", "content": "salida vieja", "tool_name": "git_status"},
        {"role": "assistant", "content": "ok"},
    ])
    assert [m.role for m in out] == ["assistant", "assistant"]
    assert out[0].content.startswith("[resultado git_status]")
    assert out[0].tool_calls == ()


def test_replay_cierra_cadena_cortada():
    """Dos llamadas, un solo resultado: la que falta se responde sintéticamente."""
    calls = [
        {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": {"path": "x"}}},
        {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": {"path": "y"}}},
    ]
    out = _replay_history([
        {"role": "assistant", "content": "", "tool_calls": calls},
        {"role": "tool", "content": "contenido x", "tool_name": "read_file", "tool_call_id": "a"},
        {"role": "user", "content": "seguí"},
    ])
    assert [m.role for m in out] == ["assistant", "tool", "tool", "user"]
    assert out[1].tool_call_id == "a"
    assert out[2].tool_call_id == "b"
    assert out[2].content == _SIN_RESULTADO
    # Los tool van inmediatamente después del assistant que los pidió.
    assert [m.role for m in out[:3]] == ["assistant", "tool", "tool"]


def test_replay_ignora_system_y_no_inventa_pares():
    out = _replay_history([
        {"role": "system", "content": "resumen viejo"},
        {"role": "user", "content": "hola"},
    ])
    assert [m.role for m in out] == ["user"]


# ---------------------------------------------------------------- el loop real


def test_el_loop_persiste_la_llamada_sin_texto(tmp_path):
    """El modelo que solo emite tool_calls (content="") deja fila con la llamada."""
    slog = _log(tmp_path)

    async def run():
        sess = slog.create_session(project="tool-calls")
        reg = ToolRegistry()
        reg.register_function(lambda text: f"echo:{text}", name="echo", description="echo")
        router = ProviderRouter()
        router.register_provider("mock", ToolThenFinalProvider())
        router.register_model(ModelRoute(id="tool-model", provider_name="mock", tier=3))
        loop = AgentLoop(
            provider_router=router,
            tool_registry=reg,
            budget=IterationBudget(max_iterations=5),
        )
        loop.session_log = slog
        return sess, await loop.run(task="usa la tool echo", session_id=sess.id, hint="coding")

    try:
        sess, res = asyncio.run(run())
        assert res.success
        msgs = slog.get_messages(sess.id)
        asst = [m for m in msgs if m.role == "assistant" and m.tool_calls]
        tools = [m for m in msgs if m.role == "tool"]
        assert len(asst) == 1, [m.role for m in msgs]
        assert asst[0].content == ""
        assert asst[0].tool_calls[0]["function"]["name"] == tools[0].tool_name
        assert asst[0].tool_calls[0]["id"] == tools[0].tool_call_id
        # Y al reanudar, el par se reconstruye (no como nota).
        replay = _replay_history([m.to_dict() for m in msgs])
        roles = [m.role for m in replay]
        assert "tool" in roles
        assert not any((m.content or "").startswith("[resultado ") for m in replay)
        idx = roles.index("tool")
        assert replay[idx - 1].tool_calls
    finally:
        slog.close()


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_branch_copia_los_tool_calls(tmp_path, role):
    """`branch(copy_messages=True)` replica filas, incluidas las llamadas."""
    slog = _log(tmp_path)
    try:
        sess = slog.create_session()
        slog.append(sess.id, Message(role=role, content="", tool_calls=CALLS))
        hija = slog.branch(sess.id, copy_messages=True)
        copia = slog.get_messages(hija.id)
        assert copia and copia[0].tool_calls == CALLS
    finally:
        slog.close()
