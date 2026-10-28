"""El borrado de una sesión no puede dejar rastro.

Un `DELETE FROM sessions` limpia mensajes, FTS y compaction_state, pero la
sesión sigue existiendo en `todos`, `goals`, `approval_grants` y en tres
directorios con nombres derivados por hash. Estos tests fijan que borrar deje
la base y el disco exactamente como estaban antes de crear la sesión.

Todo corre contra `tmp_path`: la raíz de artefactos se deriva del directorio de
la base, así que estos tests no pueden tocar `data/` real.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.core import checkpoints, git_checkpoints, session_janitor as j, spill
from src.core.approvals import GrantStore
from src.core.goal_store import GoalStore
from src.core.session_log import Message, SessionLog
from src.core.todo_store import TodoStore


@pytest.fixture()
def db(tmp_path):
    log = SessionLog(str(tmp_path / "session.db"))
    yield tmp_path / "session.db", log
    log.close()


def _sesion_con_todo(db_path, log, project="pytest"):
    sid = log.create_session(project=project).id
    log.append(sid, Message(role="user", content="hola"))
    log.append(sid, Message(role="assistant", content="chau"))
    TodoStore(db_path=db_path).write(
        sid, [{"content": "una tarea", "status": "pending"}])
    GoalStore(db_path).set(sid, "un objetivo")
    GrantStore(str(db_path.parent / "approvals.db")).add(
        session_id=sid, tool="write_file", action="write", key_hash="k1",
        decision="allow", scope="session")
    for path in (checkpoints.session_dir(sid), git_checkpoints.repo_dir(sid),
                 spill.spill_dir(sid)):
        # Las rutas que calculan los módulos apuntan a data/ real: acá se usa la
        # raíz temporal con el MISMO nombre, que es lo que resuelve el janitor.
        d = db_path.parent / path.parent.name / path.name
        d.mkdir(parents=True, exist_ok=True)
        (d / "artefacto.txt").write_text("x", encoding="utf-8")
    return sid


def test_collect_ve_todo_lo_que_deja_una_sesion(db):
    db_path, log = db
    sid = _sesion_con_todo(db_path, log)

    info = j.collect(sid, db_path=db_path, events=True)

    assert info["exists"] is True
    por_store = {a["store"]: a["count"] for a in info["artifacts"]}
    assert por_store["sessions"] == 1
    assert por_store["messages"] == 2
    assert por_store["todos"] == 1
    assert por_store["goals"] == 1
    assert por_store["approval_grants"] == 1
    assert por_store["checkpoints"] == 1
    assert por_store["git_checkpoints"] >= 1
    assert por_store["spill"] == 1
    # `messages`/`compaction_state` caen por FK; el resto es lo que se acumulaba.
    assert set(info["leftovers"]) == {"todos", "goals", "approval_grants",
                                      "checkpoints", "git_checkpoints", "spill"}


def test_delete_session_no_deja_ni_filas_ni_directorios(db):
    db_path, log = db
    sid = _sesion_con_todo(db_path, log)
    roots = j.dir_roots(db_path.parent)
    dirs = {s: j.artifact_dir(s, sid, roots)
            for s in ("checkpoints", "git_checkpoints", "spill")}
    assert all(d.is_dir() for d in dirs.values()), dirs

    res = j.delete_session(sid, db_path=db_path, events=True)

    assert res["ok"] is True, res["errors"]
    assert res["deleted"]["sessions"] == 1
    assert res["deleted"]["messages"] == 2

    info = j.collect(sid, db_path=db_path, events=True)
    assert info["exists"] is False
    assert [a for a in info["artifacts"] if a["count"]] == []
    assert info["leftovers"] == []

    assert not any(d.exists() for d in dirs.values())

    audit = j.audit(db_path=db_path, legacy_spill=False)
    assert audit["ok"] is True
    assert audit["totals"]["errors"] == 0
    assert audit["totals"]["warnings"] == 0


def test_delete_session_preserva_el_arbol(db):
    db_path, log = db
    padre = log.create_session(project="pytest").id
    hijo = log.create_session(project="pytest", parent_id=padre).id

    with pytest.raises(j.SessionHasChildren) as exc:
        j.delete_session(padre, db_path=db_path)
    assert exc.value.children == [hijo]

    # El padre sobrevive intacto: el intento fallido no borró nada suyo.
    assert j.collect(padre, db_path=db_path)["exists"] is True
    assert j.delete_session(hijo, db_path=db_path)["deleted"]["sessions"] == 1
    assert j.delete_session(padre, db_path=db_path)["deleted"]["sessions"] == 1


def test_delete_session_inexistente_es_error_explicito(db):
    db_path, _log = db
    with pytest.raises(j.SessionNotFound):
        j.delete_session("no-existe-xyz", db_path=db_path)


def test_purge_dry_run_no_borra_nada(db):
    db_path, log = db
    viva = log.create_session(project="pytest").id
    # Estado huérfano de una sesión que ya no está (lo que dejaba el DELETE viejo).
    TodoStore(db_path=db_path).write(
        "fantasma-1234", [{"content": "vieja", "status": "pending"}])

    plan = j.purge(db_path=db_path)
    assert plan["dry_run"] is True
    assert plan["plan"]["todos"] == 1
    assert TodoStore(db_path=db_path).read("fantasma-1234")["todos"]

    hecho = j.purge(apply=True, db_path=db_path)
    assert hecho["dry_run"] is False
    assert hecho["deleted"]["todos"] == 1
    assert TodoStore(db_path=db_path).read("fantasma-1234")["todos"] == []
    # La sesión viva no se toca.
    assert j.collect(viva, db_path=db_path)["exists"] is True


def test_purge_limpia_directorios_huerfanos_y_deja_los_vivos(db):
    db_path, log = db
    viva = log.create_session(project="pytest").id

    raiz = db_path.parent / "checkpoints"
    (raiz / "fantasma-1234").mkdir(parents=True)
    (raiz / "fantasma-1234" / "snap.txt").write_text("x", encoding="utf-8")
    (raiz / viva).mkdir(parents=True)

    plan = j.purge(apply=True, db_path=db_path)
    assert plan["deleted"]["checkpoints"] == 1
    assert not (raiz / "fantasma-1234").exists()
    assert (raiz / viva).is_dir()


def test_audit_detecta_lo_que_dejaba_el_delete_viejo(db):
    db_path, log = db
    sid = log.create_session(project="pytest").id
    TodoStore(db_path=db_path).write(
        sid, [{"content": "una tarea", "status": "pending"}])
    GoalStore(db_path).set(sid, "un objetivo")

    # El DELETE viejo: sólo la fila, que es lo que hacía delete_session.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()
    conn.close()

    audit = j.audit(db_path=db_path, legacy_spill=False)
    codigos = {f["code"]: f["count"] for f in audit["findings"] if f["count"]}
    assert codigos["orphan_todos"] == 1
    assert codigos["orphan_goals"] == 1
    assert audit["ok"] is True          # huérfano != inconsistencia
    assert audit["totals"]["warnings"] >= 2

    j.purge(apply=True, db_path=db_path)
    despues = {f["code"]: f["count"] for f in j.audit(
        db_path=db_path, legacy_spill=False)["findings"] if f["count"]}
    assert "orphan_todos" not in despues
    assert "orphan_goals" not in despues


def test_purge_con_base_vacia_no_borra_directorios(db):
    """Sin ninguna sesión viva no se puede atribuir un directorio: no se toca.

    Es la diferencia entre "base recién limpiada" y "base movida/perdida". Si
    purgar borrara todo `data/checkpoints` en ese estado, no tendría vuelta atrás.
    """
    db_path, _log = db
    raiz = db_path.parent / "checkpoints"
    (raiz / "de-otra-instalacion").mkdir(parents=True)
    (raiz / "de-otra-instalacion" / "snap.txt").write_text("x", encoding="utf-8")

    res = j.purge(apply=True, db_path=db_path)
    assert res["deleted"]["checkpoints"] == 0
    assert (raiz / "de-otra-instalacion").is_dir()
    assert any("force" in e for e in res["errors"])

    res = j.purge(apply=True, db_path=db_path, force=True)
    assert res["deleted"]["checkpoints"] == 1
    assert not (raiz / "de-otra-instalacion").exists()


def test_compactacion_guarda_en_la_carpeta_de_la_sesion(db, monkeypatch):
    """Lo que la compactación descarta cae en la sesión y se borra con ella.

    Antes escribía `data/spill/spill_<ts>_<hash>.txt` plano, sin id: no había
    forma de saber de qué sesión era ni de borrarlo al borrar la sesión, y se
    acumulaba para siempre como "volcado sin dueño".
    """
    import asyncio

    from src.core.compaction import CompactionPipeline

    db_path, log = db
    # La raíz de spill tiene que ser la misma que deriva el janitor del archivo
    # de la base (el conftest la redirige al tmp de pytest, que es otro lado).
    monkeypatch.setattr(spill, "_spill_root", lambda: db_path.parent / "spill")
    sid = log.create_session(project="pytest").id
    pipe = CompactionPipeline({
        "micro": {"keep_recent": 0},
        "budget": {"max_tool_result_chars": 50},
    })
    grande = "x" * 5000
    mensajes = [
        {"role": "system", "content": "sistema"},
        {"role": "tool", "content": grande, "tool_call_id": "c1"},
    ]

    asyncio.run(pipe.compact(mensajes, limit=100, session_id=sid,
                             include_summary_fallback=False))

    carpeta = spill.spill_dir(sid)
    assert carpeta.is_dir()
    assert list(carpeta.glob("compaction-*.txt"))
    # El mensaje apunta al archivo real, no a un directorio genérico.
    info = j.collect(sid, db_path=db_path)
    assert {a["store"]: a["count"] for a in info["artifacts"]}["spill"] >= 1

    j.delete_session(sid, db_path=db_path)
    assert not carpeta.exists()


def test_audit_no_escribe(db):
    db_path, log = db
    sid = log.create_session(project="pytest").id
    log.append(sid, Message(role="user", content="hola"))
    antes = db_path.stat().st_size

    j.audit(db_path=db_path, legacy_spill=False)
    j.audit(db_path=db_path, legacy_spill=False)

    assert db_path.stat().st_size == antes
    assert j.collect(sid, db_path=db_path)["exists"] is True
    # Una base que no existe no se crea por auditarla.
    inexistente = db_path.parent / "otra.db"
    j.audit(db_path=inexistente, legacy_spill=False)
    assert not inexistente.exists()
