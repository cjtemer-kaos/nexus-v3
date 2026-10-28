"""todo_read: la lista de tareas era de SOLO escritura para el modelo.

`todo_write` guarda la lista por sesion, pero la unica forma de leerla era
`GET /api/todos?session_id=` (HTTP). Dentro de un turno el modelo no tenia
ninguna tool para releerla: despues de una compactacion (o al retomar una
sesion) veia su propia lista en el historial pero no podia reconstruirla, y
opencode si tiene `todoread`.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import server as S  # noqa: E402
from src.core.tool_registry import TOOL_ALIASES  # noqa: E402


@pytest.fixture(autouse=True)
def _base_temporal(tmp_path, monkeypatch):
    """`todo_write`/`todo_read` escriben en la base del server: que sea en tmp.

    Con la base real, cada corrida dejaba filas `test-todo-*` en `data/` que
    quedaban para siempre porque `todos` no tiene FK.
    """
    from src.core.session_log import SessionLog

    log = SessionLog(str(tmp_path / "session.db"))
    monkeypatch.setitem(S._state, "session_log", log)
    yield
    log.close()


@pytest.fixture()
def registrar():
    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)

    def build(sid: str):
        reg = S._build_registry(scope, sid, cfg)

        def llamar(name, args=None):
            return asyncio.run(reg.dispatch(name, args or {}, scope=scope, hooks=None))

        return reg, llamar

    return build


def test_todo_read_sin_lista_lo_dice_en_vez_de_devolver_vacio(registrar):
    _, llamar = registrar(f"test-todo-{uuid.uuid4().hex[:8]}")

    out = llamar("todo_read")

    assert "No hay lista" in out


def test_todo_write_y_todo_read_comparten_sesion(registrar):
    sid = f"test-todo-{uuid.uuid4().hex[:8]}"
    _, llamar = registrar(sid)

    llamar("todo_write", {"todos": [
        {"content": "medir el defecto", "status": "completed"},
        {"content": "escribir el fix", "status": "in_progress"},
        {"content": "correr la suite", "status": "pending"},
    ]})
    out = llamar("todo_read")

    assert "[x] medir el defecto" in out
    assert "[~] escribir el fix" in out
    assert "[ ] correr la suite" in out
    assert "pending=1 in_progress=1 completed=1" in out


def test_todo_read_es_por_sesion(registrar):
    _, a = registrar(f"test-todo-a-{uuid.uuid4().hex[:8]}")
    _, b = registrar(f"test-todo-b-{uuid.uuid4().hex[:8]}")

    a("todo_write", {"todos": [{"content": "solo de A", "status": "pending"}]})

    assert "solo de A" in a("todo_read")
    assert "No hay lista" in b("todo_read")


def test_todo_write_reemplaza_la_lista_entera(registrar):
    _, llamar = registrar(f"test-todo-{uuid.uuid4().hex[:8]}")

    llamar("todo_write", {"todos": [{"content": "vieja", "status": "pending"}]})
    llamar("todo_write", {"todos": [{"content": "nueva", "status": "completed"}]})
    out = llamar("todo_read")

    assert "nueva" in out
    assert "vieja" not in out


def test_alias_todoread(registrar):
    _, llamar = registrar(f"test-todo-{uuid.uuid4().hex[:8]}")

    assert TOOL_ALIASES["todoread"] == "todo_read"
    assert "No hay lista" in llamar("todoread")


def test_todo_write_sigue_validando(registrar):
    _, llamar = registrar(f"test-todo-{uuid.uuid4().hex[:8]}")

    with pytest.raises(ValueError):
        llamar("todo_write", {"todos": [{"content": "", "status": "pending"}]})
    with pytest.raises(ValueError):
        llamar("todo_write", {"todos": [{"content": "x", "status": "inventado"}]})


def test_todo_write_con_string_no_revienta(registrar):
    """Un modelo chico manda `todos` como string: el registry lo coerciona a
    ["no-es-lista"] y el handler hacia `item.get` -> AttributeError sin mensaje.
    Tiene que ser un ValueError que diga la forma esperada."""
    _, llamar = registrar(f"test-todo-{uuid.uuid4().hex[:8]}")

    with pytest.raises(ValueError) as exc:
        llamar("todo_write", {"todos": "no-es-lista"})
    assert "todos[0]" in str(exc.value) and "content" in str(exc.value)

    # La lista previa no se toca cuando el write falla.
    llamar("todo_write", {"todos": [{"content": "sigue", "status": "pending"}]})
    with pytest.raises(ValueError):
        llamar("todo_write", {"todos": ["texto suelto"]})
    assert "sigue" in llamar("todo_read")


@pytest.mark.parametrize(
    "todos, esperado",
    [
        (["suelto"], "todos[0]"),
        ([{"content": 123, "status": "pending"}], "content"),
        ([{"content": "ok", "status": 1}], "status"),
        ([{"content": "ok"}], "status"),
        ([{"status": "pending"}], "content"),
    ],
)
def test_todo_write_items_mal_formados(registrar, todos, esperado):
    _, llamar = registrar(f"test-todo-{uuid.uuid4().hex[:8]}")
    with pytest.raises(ValueError) as exc:
        llamar("todo_write", {"todos": todos})
    assert esperado in str(exc.value)
    assert isinstance(exc.value, ValueError)
