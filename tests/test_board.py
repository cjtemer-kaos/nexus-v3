"""Tests del board: carrera del claim, lease y migracion de esquema.

El claim es claim-once: el veredicto lo da un UPDATE condicional, no una
lectura previa. Estos tests fuerzan el peor caso a proposito.
"""
from __future__ import annotations

import sqlite3
import threading

from helpers import remove_path
from src.core.board import TASK_LEASE_S, Board


def _fresh(db: str) -> None:
    for suf in ("", "-wal", "-shm"):
        remove_path(db + suf)


def test_claim_es_atomico_bajo_concurrencia(monkeypatch):
    """Con 6 agentes reclamando a la vez, gana exactamente uno.

    La barrera los obliga a leer la tarea `open` ANTES de que ninguno escriba
    (el peor caso). Sin el UPDATE condicional, ganarian todos.
    """
    db = "./data/_board_race_pytest.db"
    _fresh(db)
    b = Board(db)
    t = b.create("carrera", priority=1)

    n = 6
    barrier = threading.Barrier(n)
    real_get = Board.get
    ya_paso: set[str] = set()
    guard = threading.Lock()

    def get_con_barrera(self, tid):
        r = real_get(self, tid)
        name = threading.current_thread().name
        with guard:
            primero = name not in ya_paso
            ya_paso.add(name)
        if primero and name.startswith("agente"):
            barrier.wait(timeout=15)  # todos leyeron: ahora corren juntos al UPDATE
        return r

    monkeypatch.setattr(Board, "get", get_con_barrera)

    resultados: list[tuple[str, bool]] = []
    errores: list[str] = []
    rlock = threading.Lock()

    def worker(name: str) -> None:
        try:
            ok = bool(b.claim(t["id"], name)["ok"])
        except Exception as e:  # noqa: BLE001 - se reporta el error al assert
            with rlock:
                errores.append(f"{name}: {type(e).__name__}: {e}")
            return
        with rlock:
            resultados.append((name, ok))

    hilos = [threading.Thread(target=worker, args=(f"agente{i}",), name=f"agente{i}") for i in range(n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)

    assert not errores, errores
    assert len(resultados) == n, resultados
    ganadores = [nom for nom, ok in resultados if ok]
    assert len(ganadores) == 1, f"ganaron {len(ganadores)}: {resultados}"

    final = b.get(t["id"])
    assert final["status"] == "claimed"
    assert final["assignee"] == ganadores[0]
    assert final["lease_expires_at"] > final["updated_at"]


def test_reclamo_ajeno_con_lease_vivo_es_rechazado():
    db = "./data/_board_lease_pytest.db"
    _fresh(db)
    b = Board(db)
    t = b.create("lease", priority=1)

    assert b.claim(t["id"], "a", lease_s=60)["ok"]
    r = b.claim(t["id"], "b")
    assert not r["ok"] and "ya reclamada por a" in r["error"]
    assert b.get(t["id"])["assignee"] == "a"


def test_heartbeat_renueva_el_lease():
    db = "./data/_board_hb_pytest.db"
    _fresh(db)
    b = Board(db)
    t = b.create("hb", priority=1)

    assert b.claim(t["id"], "a", lease_s=60)["ok"]
    antes = b.get(t["id"])["lease_expires_at"]
    assert b.claim(t["id"], "a", lease_s=120)["ok"]  # mismo agente: heartbeat
    assert b.get(t["id"])["lease_expires_at"] > antes


def test_lease_vencido_habilita_a_otro_agente():
    db = "./data/_board_exp_pytest.db"
    _fresh(db)
    b = Board(db)
    t = b.create("expirado", priority=1)

    assert b.claim(t["id"], "a", lease_s=0.0)["ok"]  # nace vencido
    r = b.claim(t["id"], "b")
    assert r["ok"] and b.get(t["id"])["assignee"] == "b"


def test_update_terminal_libera_el_claim():
    db = "./data/_board_done_pytest.db"
    _fresh(db)
    b = Board(db)
    t = b.create("cerrar", priority=1)

    assert b.claim(t["id"], "a")["ok"]
    assert b.update(t["id"], status="done")["task"]["lease_expires_at"] is None
    r = b.claim(t["id"], "b")
    assert not r["ok"] and "terminal" in r["error"]


def test_migra_board_con_esquema_viejo():
    """Un board creado antes del lease se migra al abrirlo (sin perder datos)."""
    db = "./data/_board_migra_pytest.db"
    _fresh(db)
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT,"
        "assignee TEXT, created_by TEXT, priority INTEGER, deps TEXT, created_at REAL,"
        "updated_at REAL, notes TEXT)")
    con.execute("INSERT INTO tasks VALUES('legacy','viejo','','claimed','a','',3,'',1.0,2.0,'')")
    con.commit()
    con.close()

    b = Board(db)  # migra al abrir
    t = b.get("legacy")
    assert t is not None
    assert t["lease_expires_at"] == 2.0 + TASK_LEASE_S  # backfill desde updated_at
    assert [x["id"] for x in b.list(status="claimed")] == ["legacy"]
