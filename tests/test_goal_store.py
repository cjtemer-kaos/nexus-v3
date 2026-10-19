"""Tests del GoalStore: validacion, last-write-wins y aislamiento por sesion."""
import pytest

from src.core.goal_store import GoalStore, VALID_STATUSES, MAX_OBJECTIVE


@pytest.fixture
def store(tmp_path):
    return GoalStore(tmp_path / "goals.db")


def test_valid_statuses_are_the_documented_three():
    assert VALID_STATUSES == ("active", "done", "cleared")


def test_set_and_get_roundtrip(store):
    g = store.set("s1", "terminar el harness 100% funcional")
    assert g["exists"] is True
    assert g["objective"] == "terminar el harness 100% funcional"
    assert g["status"] == "active"
    assert g["updated_at"]
    assert store.get("s1") == g


def test_get_unknown_session_is_not_created(store):
    g = store.get("nope")
    assert g == {"session_id": "nope", "exists": False, "objective": "", "status": "none"}


def test_last_write_wins(store):
    store.set("s1", "objetivo viejo")
    g = store.set("s1", "objetivo nuevo")
    assert g["objective"] == "objetivo nuevo"
    assert store.get("s1")["objective"] == "objetivo nuevo"


def test_sessions_are_isolated(store):
    store.set("s1", "objetivo uno")
    store.set("s2", "objetivo dos")
    assert store.get("s1")["objective"] == "objetivo uno"
    assert store.get("s2")["objective"] == "objetivo dos"


def test_clear_removes_and_reports(store):
    store.set("s1", "objetivo")
    assert store.clear("s1") is True
    assert store.clear("s1") is False
    assert store.get("s1")["exists"] is False


def test_rejects_empty_objective(store):
    with pytest.raises(ValueError):
        store.set("s1", "   ")
    with pytest.raises(ValueError):
        store.set("", "objetivo")


def test_rejects_invalid_status_and_long_objective(store):
    with pytest.raises(ValueError):
        store.set("s1", "objetivo valido", status="raro")
    with pytest.raises(ValueError):
        store.set("s1", "x" * (MAX_OBJECTIVE + 1))
