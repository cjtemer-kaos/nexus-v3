"""Tests Fase 9 — Ciclo de autoaprendizaje P1 re-wired (ciclo #113, colmena #132).

Verifican los 4 pasos + scheduler con componentes aislados en tmp:
  1) telemetría por turno -> EventStore
  2) judge -> Thompson (AdaptiveRouter persistente) + SelfLearningLoop batch
  3) insights -> LearningGraph con aristas SUPERSEDES
  4) SkillOpt -> gate held-out fail-closed
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Componentes del ciclo apuntando a tmp (nunca al brain/datos reales)."""
    monkeypatch.setattr("src.core.adaptive_router._ROUTER_STORAGE",
                        tmp_path / "router_state.json")
    monkeypatch.setattr("src.core.self_learning_loop.brain_path",
                        lambda: tmp_path)

    from src.core.event_store import EventStore
    from src.core.learning_graph import LearningGraph, reset_singleton
    from src.core.learning_service import LearningService
    from src.core.knowledge_memory import KnowledgeMemory
    from src.core.skill_opt import SkillOptimizer

    reset_singleton()
    events = EventStore(db_path=tmp_path / "events.db")
    graph = LearningGraph(db_path=tmp_path / "graph.db")
    knowledge_memory = KnowledgeMemory(
        db_path=tmp_path / "knowledge_graph.db", rag_index=False
    )
    skillopt = SkillOptimizer(state_path=tmp_path / "skill_opt_state.json",
                              heldout_path=tmp_path / "heldout.json")
    svc = LearningService(events=events, graph=graph, skill_opt=skillopt,
                          knowledge_memory=knowledge_memory, interval_s=999.0)
    return svc, tmp_path


def test_record_turn_telemetria_y_thompson(isolated):
    svc, tmp_path = isolated
    r = _run(svc.record_turn(
        task="escribe una funcion en python", output="def f(): return 1",
        model="qwen2.5-coder:7b", success=True, session_id="s1"))

    assert r["recorded"] is True
    assert r["model"] == "qwen2.5-coder:7b"
    # paso 1: evento persistido en EventStore
    assert svc.events.count(kind="task_completed") == 1
    ev = svc.events.list_events(kind="task_completed", limit=1)[0]
    assert ev.data["model"] == "qwen2.5-coder:7b"
    assert ev.data["success"] is True
    # paso 2: Thompson tiene el outcome del modelo
    stats = svc.router.get_stats()
    models = stats.get("models", stats)
    assert "qwen2.5-coder:7b" in models
    # estado persistido en disco (flush explicito: el throttle es cada 10)
    svc.router.flush()
    assert (tmp_path / "router_state.json").exists()


def test_record_turn_nunca_rompe(isolated):
    """El aprendizaje no puede tumbar el chat: todo es best-effort."""
    svc, _ = isolated
    svc.judge = object()  # judge roto a proposito (sin metodo judge_response)
    svc.router = type("R", (), {"record_result": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))})()

    r = _run(svc.record_turn(task="hola", output="ok", model="m", success=True))
    assert r["recorded"] is True
    assert svc._counters["turns"] == 1


def test_batch_cycle_alimenta_grafo_con_supersedes(isolated):
    svc, tmp_path = isolated
    # memoria del batch cableada al grafo (paso 3)
    svc._store_pattern("k1", "[high_quality|coding] model=a score=0.95")
    svc._store_pattern("k2", "[high_quality|coding] model=b score=0.40")

    concepts = svc.graph.list_nodes("CONCEPT")
    assert len(concepts) == 2
    fam_nodes = [n for n in concepts if n.description == "family=high_quality|coding"]
    assert len(fam_nodes) == 2
    edges = svc.graph.get_edges()
    assert any(e.relationship.value == "SUPERSEDES" for e in edges)

    # El insight también debe quedar en el grafo durable que consulta GraphView.
    kg = svc.knowledge_memory
    assert kg.status()["nodes"] == 2
    with sqlite3.connect(str(tmp_path / "knowledge_graph.db")) as con:
        typed = con.execute(
            "SELECT count(*) FROM edges WHERE relation='supersedes'"
        ).fetchone()[0]
    assert typed == 1
    assert svc.stats()["knowledge_graph"]["nodes"] == 2

    # mismo patron dos veces => dedupe (experiencia, no duplicado)
    svc._store_pattern("k3", "[high_quality|coding] model=a score=0.95")
    assert len(svc.graph.list_nodes("CONCEPT")) == 2
    assert kg.status()["nodes"] == 2


def test_self_learning_batch_process(isolated):
    svc, _ = isolated

    async def go():
        await svc.start(enable_dream=False)
        await svc.record_turn(task="investiga X", output="resultado",
                              model="m1", success=True)
        # dispara el ciclo batch manualmente (sin esperar 120s)
        from src.core.actor_base import ActorMessage
        await svc.learning_loop.handle_message(ActorMessage(msg_type="cycle"))
        st = svc.learning_loop.get_stats()
        await svc.stop()
        return st

    st = _run(go())
    assert st["cycles"] >= 1
    assert st["pending_records"] == 0  # procesados y persistidos
    # SQLite de learning_loop.db en el (tmp) brain
    import sqlite3
    db = Path(svc.learning_loop._db_path)
    assert db.exists()
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM learning_records").fetchone()[0]
    conn.close()
    assert n >= 1
    assert svc.knowledge_memory.status()["nodes"] >= 1


def test_graph_maintenance_scheduler(isolated, monkeypatch):
    svc, _ = isolated
    calls = []

    class FakeKnowledgeMemory:
        def maintain(self):
            calls.append("maintain")
            return {"ok": True}

    monkeypatch.setattr(
        "src.core.knowledge_memory.get_knowledge_memory",
        lambda: FakeKnowledgeMemory(),
    )

    async def go():
        await svc.start(enable_dream=False, graph_maintenance_interval_s=0.01)
        # Poll hasta que corra (antes sleep fijo de 40 ms: flaky bajo carga).
        for _ in range(200):
            if calls:
                break
            await asyncio.sleep(0.01)
        await svc.stop()

    _run(go())
    assert calls, "maintenance scheduler should run after its configured interval"


def test_skillopt_gate_heldout(isolated):
    svc, tmp = isolated
    sk = svc.skill_opt

    sk.propose("cambio bueno", {"prompt": "v2"}, change_id="good")
    ok = _run(sk.validate("good", lambda case: True))
    assert ok["status"] == "accepted"
    assert ok["persisted"] is True

    sk.propose("cambio malo", {"prompt": "v3"}, change_id="bad")
    bad = _run(sk.validate("bad", lambda case: False))
    assert bad["status"] == "rejected"
    assert bad["persisted"] is False

    # solo el aceptado cuenta como cambio aplicado
    acc = sk.accepted()
    assert [c["change_id"] for c in acc] == ["good"]
    st = sk.stats()
    assert st["accepted"] == 1
    assert st["by_status"] == {"accepted": 1, "rejected": 1}
    assert st["baseline_score"] == 1.0
    # estado en disco
    state = json.loads((tmp / "skill_opt_state.json").read_text(encoding="utf-8"))
    assert state["changes"]["good"]["status"] == "accepted"
    assert state["changes"]["bad"]["status"] == "rejected"


def test_skillopt_validate_async_evaluator_y_heldout(isolated):
    svc, _ = isolated
    sk = svc.skill_opt
    sk.propose("async change", {}, change_id="async1")

    async def evaluator(case):
        await asyncio.sleep(0)
        return case["id"] == "h1"  # solo la mitad

    res = _run(sk.validate("async1", evaluator, required_score=1.0))
    assert res["status"] == "rejected"  # 0.5 < 1.0 -> fail-closed

    res2 = _run(sk.validate("async1", evaluator, required_score=0.5))
    assert res2["status"] == "accepted"


def test_skillopt_heldout_custom(isolated):
    svc, _ = isolated
    sk = svc.skill_opt
    assert len(sk.heldout()) >= 1  # seed
    n = sk.set_heldout([{"task": "di hola", "expect_contains": "hola"}])
    assert n == 1
    with pytest.raises(ValueError):
        sk.set_heldout([])


def test_stats_y_recommend(isolated):
    svc, _ = isolated
    _run(svc.record_turn(task="tarea", output="ok", model="mX", success=True))
    st = svc.stats()
    assert st["turns"] == 1
    assert st["success"] == 1
    assert st["events_task_completed"] == 1
    assert st["started"] is False  # start() no llamado en este test
    assert "self_learning" in st and "router" in st and "skill_opt" in st

    rec = svc.recommend("programa codigo python")
    assert "model" in rec


def test_start_stop_scheduler(isolated):
    svc, tmp_path = isolated

    async def go():
        await svc.start(enable_dream=False)
        assert svc.stats()["started"] is True
        await svc.record_turn(task="t", output="o", model="mY", success=True)
        await svc.stop()
        assert svc.stats()["started"] is False

    _run(go())
    # stop() debe haber flusheado el estado del Thompson al disco
    assert (tmp_path / "router_state.json").exists()


def test_dream_best_effort(isolated, monkeypatch):
    svc, _ = isolated
    import src.core.dream_distill as dd

    class Boom:
        def __init__(self, **kw):
            pass

        async def dream(self):
            raise RuntimeError("dream roto")

    monkeypatch.setattr(dd, "DreamDistillEngine", Boom)
    res = _run(svc.run_dream())
    assert res["status"] == "failed"
