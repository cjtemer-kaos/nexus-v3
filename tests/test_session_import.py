"""Importación de sesiones externas: handle opaco, preview y escritura real.

Fija tres cosas: el cliente nunca manda una ruta (handle validado contra las
raíces), el preview muestra lo que se descarta (tools/razonamiento) y `aplicar`
crea una sesión propia con los turnos en orden.
"""
from __future__ import annotations

import base64
import json

import pytest

from src.core import session_import as imp
from src.core.session_log import SessionLog


def _lineas_jsonl(path, objetos):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in objetos) + "\n",
                    encoding="utf-8")
    return path


@pytest.fixture()
def raices(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    claude.mkdir()
    codex.mkdir()
    monkeypatch.setattr(imp, "_raices", lambda: {"claude": [claude], "codex": [codex]})
    return claude, codex


def test_claude_lista_preview_y_descarta_tools(raices):
    claude, _ = raices
    _lineas_jsonl(claude / "proj" / "abc.jsonl", [
        {"type": "summary", "summary": "x"},
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "user", "cwd": "D:/ias", "sessionId": "abc", "timestamp": "2026-10-01T10:00:00Z",
         "message": {"role": "user", "content": "¿Cómo compilo el proyecto?"}},
        {"type": "assistant", "equipo": "x", "timestamp": "2026-10-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-x", "content": [
             {"type": "thinking", "thinking": "no mostrar"},
             {"type": "text", "text": "Con make."},
             {"type": "tool_use", "name": "Bash", "input": {"cmd": "make"}},
         ]}},
        {"type": "user", "timestamp": "2026-10-01T10:00:09Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-10-01T10:00:10Z",
         "message": {"role": "assistant", "content": ""}},
    ])

    listado = imp.listar(source="claude", limit=10)
    assert len(listado["sessions"]) == 1
    fila = listado["sessions"][0]
    assert fila["cwd"] == "D:/ias"
    assert fila["title"] == "¿Cómo compilo el proyecto?"
    assert "claude" not in fila["handle"]
    assert "abc.jsonl" not in fila["handle"], "el handle no expone la ruta"

    p = imp.preview(fila["handle"], limit=10)
    assert p["model"] == "claude-x" and p["external_session"] == "abc"
    assert [t["role"] for t in p["turns"]] == ["user", "assistant"]
    assert p["turns"][1]["content"] == "Con make."
    assert p["skipped"]["tool_use"] == 1
    assert p["skipped"]["tool_result"] == 1
    assert p["skipped"]["thinking"] == 1
    # El turno que sólo traía un tool_result y el assistant con content vacío.
    assert p["skipped"]["sin_texto"] == 2


def test_codex_usa_response_item_y_descarta_contexto(raices):
    _, codex = raices
    _lineas_jsonl(codex / "2026" / "10" / "01" / "rollout-x.jsonl", [
        {"type": "session_meta", "payload": {"id": "sess-9", "cwd": "D:/proj"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                              "content": [{"type": "input_text", "text": "reglas"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text",
                                                           "text": "<environment_context>\n<cwd>x</cwd>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "arreglá el test"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "Listo."}]}},
    ])
    fila = imp.listar(source="codex", limit=5)["sessions"][0]
    p = imp.preview(fila["handle"], limit=10)
    assert p["cwd"] == "D:/proj" and p["external_session"] == "sess-9"
    assert [t["content"] for t in p["turns"]] == ["arreglá el test", "Listo."]
    assert p["skipped"]["rol_developer"] == 1
    assert p["skipped"]["contexto_entorno"] == 1
    assert p["skipped"]["function_call"] == 1


def test_codex_viejo_cae_a_event_msg(raices):
    _, codex = raices
    _lineas_jsonl(codex / "r.jsonl", [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "hola"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "buenas"}},
    ])
    p = imp.preview(imp.listar(source="codex", limit=5)["sessions"][0]["handle"])
    assert [(t["role"], t["content"]) for t in p["turns"]] == [
        ("user", "hola"), ("assistant", "buenas")]


def test_handle_no_sale_de_la_raiz(raices, tmp_path):
    claude, _ = raices
    _lineas_jsonl(claude / "a.jsonl", [{"type": "user", "message": {"content": "x"}}])
    secreto = tmp_path / "secreto.jsonl"
    secreto.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError):
        imp.resolver(imp._b64("claude|0|../secreto.jsonl"))
    with pytest.raises(ValueError):
        imp.resolver(imp._b64(f"claude|0|{secreto.as_posix()}"))
    with pytest.raises(ValueError):
        imp.resolver("no-es-un-handle")
    with pytest.raises(ValueError):
        imp.resolver(imp._b64("otro|0|a.jsonl"))
    with pytest.raises(ValueError):
        imp.resolver(imp._b64("claude|0|no-existe.jsonl"))
    # Un .jsonl que no está bajo la raíz tampoco pasa por índice fuera de rango.
    with pytest.raises(ValueError):
        imp.resolver(imp._b64("claude|7|a.jsonl"))


def test_aplicar_crea_sesion_con_los_turnos(raices):
    claude, _ = raices
    _lineas_jsonl(claude / "s.jsonl", [
        {"type": "user", "cwd": "D:/ias", "timestamp": "2026-10-02T08:00:00Z",
         "message": {"content": "primer pedido"}},
        {"type": "assistant", "timestamp": "2026-10-02T08:00:01Z",
         "message": {"content": [{"type": "text", "text": "primera respuesta"}]}},
        {"type": "user", "timestamp": "2026-10-02T08:00:02Z",
         "message": {"content": "segundo pedido"}},
    ])
    fila = imp.listar(source="claude", limit=5)["sessions"][0]
    slog = SessionLog(":memory:")
    try:
        res = imp.aplicar(fila["handle"], slog)
        assert res["imported"] == 3 and res["truncated"] is False
        sess = slog.get_session(res["session_id"])
        assert sess.project == "importado-claude"
        assert sess.title == "primer pedido"
        msgs = slog.get_messages(res["session_id"], limit=10)
        assert [(m.role, m.content) for m in msgs] == [
            ("user", "primer pedido"),
            ("assistant", "primera respuesta"),
            ("user", "segundo pedido"),
        ]
        assert msgs[0].created_at.startswith("2026-10-02T08:00:00")
    finally:
        slog.close()


def test_aplicar_sin_turnos_no_crea_nada(raices):
    claude, _ = raices
    _lineas_jsonl(claude / "vacia.jsonl", [{"type": "summary", "summary": "solo metadata"}])
    fila = imp.listar(source="claude", limit=5)["sessions"][0]
    slog = SessionLog(":memory:")
    try:
        with pytest.raises(ValueError):
            imp.aplicar(fila["handle"], slog)
        assert not slog.list_sessions()
    finally:
        slog.close()


def test_fuente_desconocida_es_error(raices):
    with pytest.raises(ValueError):
        imp.listar(source="inventada")
