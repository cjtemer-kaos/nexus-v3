"""S3-fase1: session_reminders + fix typo scheduler cron."""
from __future__ import annotations

import time

import pytest

from src.core.session_reminders import MIN_EVERY_SECONDS, ReminderError, SessionReminderStore


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float):
        self.t += s


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return SessionReminderStore(session_id="s1", path=tmp_path / "s1.json", clock=clock)


def test_every_rejects_below_minimum(store):
    for bad in (60, 299, 299.9, 0, -5, float("inf"), float("nan")):
        with pytest.raises(ReminderError):
            store.add_every(bad, "msg")
    r = store.add_every(MIN_EVERY_SECONDS, "ok")
    assert r.id == "schedule-1"


def test_after_at_validators(store):
    with pytest.raises(ReminderError):
        store.add_after(0, "x")
    with pytest.raises(ReminderError):
        store.add_after(-1, "x")
    with pytest.raises(ReminderError):
        store.add_after(10, "  ")
    with pytest.raises(ReminderError):
        store.add_at(float("nan"), "x")


def test_id_no_reuso_tras_cancel(store):
    a = store.add_after(60, "a")
    assert a.id == "schedule-1"
    assert store.cancel(a.id) is True
    b = store.add_after(60, "b")
    assert b.id == "schedule-2"
    assert b.id != a.id


def test_every_latest_only_salta_backlog(store, clock):
    r = store.add_every(300, "tick")
    clock.advance(300 * 5 + 10)  # 5 periodos de backlog
    fired = store.poll()
    assert [f.id for f in fired] == [r.id]  # fold: un solo evento
    assert r.fired_count == 1
    assert r.next_fire == pytest.approx(clock.t + 300)  # salta backlog
    assert store.poll() == []  # segunda poll sin duplicados


def test_fold_multiples_vencidos_un_evento_cada_uno(store, clock):
    store.add_after(10, "a")
    store.add_at(clock.t + 10, "b")
    clock.advance(20)
    fired = store.poll()
    assert len(fired) == 2
    assert store.poll() == []  # after/at son one-shot


def test_estados_scheduled_overdue(store, clock):
    fut = store.add_at(clock.t + 3600, "fut")
    past = store.add_at(clock.t - 5, "past")
    st = store.statuses()
    assert st[fut.id] == "scheduled"
    assert st[past.id] == "overdue"


def test_fork_no_hereda(store, tmp_path, clock):
    store.add_after(60, "padre")
    child = store.fork("hija", path=tmp_path / "hija.json")
    assert child.list() == []
    assert child.statuses() == {}


def test_replay_tras_restart(tmp_path, clock):
    p = tmp_path / "s1.json"
    s1 = SessionReminderStore(session_id="s1", path=p, clock=clock)
    s1.add_every(300, "tick")
    s1.add_at(clock.t + 50, "once")
    clock.advance(300 * 4)  # restart con backlog
    s2 = SessionReminderStore(session_id="s1", path=p, clock=clock)
    assert {r.id for r in s2.list()} == {"schedule-1", "schedule-2"}
    fired = s2.poll()
    assert len(fired) == 2  # every una vez (latest-only) + at una vez
    ev = next(f for f in fired if f.kind == "every")
    assert ev.next_fire == pytest.approx(clock.t + 300)
    # ids siguen sin reusarse tras restart
    nxt = s2.add_after(60, "nuevo")
    assert nxt.id == "schedule-3"


def test_flush_barrier_escribe_archivo(store, tmp_path):
    store.add_after(60, "a")
    assert (tmp_path / "s1.json").exists()


def test_scheduler_cron_expression_parsea():
    """Regression del typo task.cronic_expression -> task.cron_expression."""
    croniter = pytest.importorskip("croniter")
    import inspect
    from src.core import scheduler as sch

    src = inspect.getsource(sch.TaskScheduler._compute_next_run)
    assert "cronic_expression" not in src
    assert "cron_expression" in src

    s = sch.TaskScheduler.__new__(sch.TaskScheduler)  # sin _load ni disco
    s.tasks, s.runs = {}, []
    now = time.time()
    task = sch.ScheduledTask({"schedule_type": "cron", "cron_expression": "*/5 * * * *", "enabled": True})
    nxt = sch.TaskScheduler._compute_next_run(s, task)
    assert nxt is not None
    # Con el typo, el AttributeError caia al fallback exacto now+3600.
    # Con el fix, la rama croniter responde (valor numerico distinto del fallback).
    assert isinstance(nxt, (int, float))
    assert abs(nxt - (now + 3600)) > 60
