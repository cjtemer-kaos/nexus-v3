"""Tests del grafo de dependencias (base de la ejecucion DAG)."""
from src.core.task_queue import DependencyGraph, TaskPriority, TaskQueue, TaskStatus


def test_levels_respect_dependencies():
    g = DependencyGraph()
    for t in ["a", "b", "c", "d"]:
        g.add_task(t)
    g.add_task("b", ["a"])
    g.add_task("c", ["a"])
    g.add_task("d", ["b", "c"])
    assert not g.has_cycle()
    levels = g.get_execution_levels()
    assert levels[0] == ["a"]
    assert set(levels[1]) == {"b", "c"}
    assert levels[2] == ["d"]


def test_topological_sort_is_a_valid_order():
    g = DependencyGraph()
    g.add_task("a")
    g.add_task("b", ["a"])
    g.add_task("c", ["b"])
    order = g.topological_sort()
    assert order.index("a") < order.index("b") < order.index("c")


def test_cycle_is_detected():
    g = DependencyGraph()
    g.add_task("a", ["b"])
    g.add_task("b", ["a"])
    assert g.has_cycle()
    # con ciclo no hay niveles completos
    assert sum(len(l) for l in g.get_execution_levels()) < 2


def test_ready_tasks_only_when_deps_completed():
    g = DependencyGraph()
    g.add_task("a")
    g.add_task("b", ["a"])
    assert g.get_ready_tasks(set()) == ["a"]
    assert set(g.get_ready_tasks({"a"})) == {"b"}
    assert g.get_ready_tasks({"a", "b"}) == []


def test_remove_task_cleans_edges():
    g = DependencyGraph()
    g.add_task("a")
    g.add_task("b", ["a"])
    g.remove_task("a")
    assert not g.has_cycle()
    assert g.get_ready_tasks(set()) == ["b"]


def test_task_queue_status_shape():
    q = TaskQueue(max_parallel=2)
    q.add_task("t1", "tarea uno", priority=TaskPriority.HIGH)
    st = q.get_status()
    assert st["total_tasks"] >= 1
    assert st["by_status"].get(TaskStatus.QUEUED.value, 0) >= 1
    assert q.get_task("t1") is not None
    assert q.get_task("t1").status is TaskStatus.QUEUED
    assert q.remove_task("t1") is True
    assert q.remove_task("t1") is False


def test_task_queue_rejects_duplicate_id():
    import pytest
    q = TaskQueue()
    q.add_task("dup", "una")
    with pytest.raises(ValueError):
        q.add_task("dup", "otra")


def test_task_queue_marks_cycle_as_failed():
    q = TaskQueue()
    q.add_task("a", "a", dependencies=["b"])
    q.add_task("b", "b", dependencies=["a"])
    assert q.get_task("b").status is TaskStatus.FAILED
    assert "cycle" in (q.get_task("b").error or "").lower()
