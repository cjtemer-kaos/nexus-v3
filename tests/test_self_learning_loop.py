"""SelfLearningLoop: ciclo de aprendizaje, judge, router y persistencia.

Modulo sin cobertura directa (la suite solo monkeypatcheaba brain_path): un bug
en _cycle se tragaba en silencio y el router nunca aprendia.
"""
import asyncio
import json
import sqlite3

import pytest

from src.core.actor_base import ActorMessage
from src.core.self_learning_loop import LearningRecord, SelfLearningLoop


@pytest.fixture()
def loop_mod(tmp_path, monkeypatch):
    import src.core.self_learning_loop as mod

    monkeypatch.setattr(mod, "brain_path", lambda: tmp_path)
    return mod


class _Router:
    def __init__(self):
        self.calls = []

    def record_result(self, model, success, quality_score):
        self.calls.append({"model": model, "success": success, "quality": quality_score})


def test_learn_message_encola_y_valida(loop_mod):
    lp = SelfLearningLoop()

    async def go():
        ok = await lp.handle_message(ActorMessage(msg_type="learn", content=json.dumps(
            {"task": "programar en python", "model": "m1", "outcome": "ok", "quality": 0.4})))
        assert ok.success is True
        bad = await lp.handle_message(ActorMessage(msg_type="learn", content="{no-json"))
        assert bad.success is False and bad.error
        unknown = await lp.handle_message(ActorMessage(msg_type="otra", content=""))
        assert unknown.success is False
        return ok

    ok = asyncio.run(go())
    assert "programar" in ok.content
    assert len(lp._records) == 1


def test_cycle_judge_router_y_persistencia(loop_mod, tmp_path):
    router = _Router()
    stored = []

    def judge(task, outcome):
        class J:
            action = "ACCEPT"
            confidence = 0.9
        return J()

    lp = SelfLearningLoop(judge_fn=judge, adaptive_router=router,
                          memory_store_fn=lambda k, v: stored.append((k, v)))
    lp._records.append(LearningRecord(task="programar python", model_used="m1",
                                      outcome="ok", quality_score=0.0, timestamp=1000.0))

    asyncio.run(lp._cycle())

    assert lp._cycles == 1
    assert router.calls and router.calls[0]["model"] == "m1"
    assert router.calls[0]["success"] is True
    assert stored, "el patron aprendido no se guardo en memoria"

    conn = sqlite3.connect(str(tmp_path / "learning_loop.db"))
    rows = conn.execute("SELECT task, model_used, quality_score, pattern FROM learning_records").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] == "m1"
    assert rows[0][2] == 0.9
    assert "high_quality" in rows[0][3] and "coding" in rows[0][3]


def test_cycle_sin_records_no_persiste(loop_mod, tmp_path):
    lp = SelfLearningLoop()
    asyncio.run(lp._cycle())
    assert lp._cycles == 1
    conn = sqlite3.connect(str(tmp_path / "learning_loop.db"))
    n = conn.execute("SELECT COUNT(*) FROM learning_records").fetchone()[0]
    conn.close()
    assert n == 0


def test_extract_pattern_clasifica(loop_mod):
    lp = SelfLearningLoop()
    p = lp._extract_pattern(LearningRecord(task="investigar docker", model_used="m",
                                           quality_score=0.1))
    assert "low_quality" in p and "research" in p and "devops" in p
    assert lp._extract_pattern(LearningRecord(task="saludar", model_used="m",
                                              quality_score=0.5)) == ""


def test_stats_y_status(loop_mod):
    lp = SelfLearningLoop(judge_fn=lambda *a: None, memory_store_fn=lambda *a: None)
    st = lp.get_stats()
    assert st["status"] == "stopped"
    assert st["has_judge"] is True and st["has_memory"] is True and st["has_router"] is False

    async def go():
        return await lp.handle_message(ActorMessage(msg_type="status", content=""))

    res = asyncio.run(go())
    assert res.success is True
    assert json.loads(res.content)["pending_records"] == 0
