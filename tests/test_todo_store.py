"""Tests del TodoStore: validacion, last-write-wins y aislamiento por sesion."""
import pytest

from src.core.todo_store import TodoStore, VALID_STATUSES


@pytest.fixture
def store(tmp_path):
    return TodoStore(tmp_path / "todos.db")


def test_valid_statuses_are_the_documented_three():
    assert VALID_STATUSES == ("pending", "in_progress", "completed")


def test_write_and_read_with_counts(store):
    result = store.write(
        "s1",
        [
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "in_progress"},
            {"content": "c", "status": "pending"},
        ],
    )
    assert [t["content"] for t in result["todos"]] == ["a", "b", "c"]
    assert result["counts"] == {"pending": 1, "inProgress": 1, "completed": 1}
    assert store.read("s1") == result


def test_read_unknown_session_is_empty(store):
    assert store.read("nope") == {
        "todos": [],
        "counts": {"pending": 0, "inProgress": 0, "completed": 0},
    }


def test_whole_list_replacement_is_last_write_wins(store):
    store.write("s1", [{"content": "a", "status": "pending"}])
    store.write("s1", [{"content": "b", "status": "completed"}])
    stored = store.read("s1")
    assert [t["content"] for t in stored["todos"]] == ["b"]
    assert stored["counts"]["completed"] == 1


def test_content_is_trimmed(store):
    out = store.write("s1", [{"content": "  con espacios  ", "status": "pending"}])
    assert out["todos"][0]["content"] == "con espacios"


@pytest.mark.parametrize(
    "todos",
    [
        [{"content": "", "status": "pending"}],
        [{"content": "   ", "status": "pending"}],
        [{"content": "a", "status": "done"}],
        [{"content": "a", "status": "pending"}, {"content": "a", "status": "completed"}],
    ],
)
def test_invalid_lists_rejected(store, todos):
    with pytest.raises(ValueError):
        store.write("s1", todos)
    assert store.read("s1")["todos"] == []


def test_parallel_limit_enforced_when_configured(tmp_path):
    strict = TodoStore(tmp_path / "strict.db", allow_parallel=False)
    with pytest.raises(ValueError):
        strict.write(
            "s1",
            [
                {"content": "a", "status": "in_progress"},
                {"content": "b", "status": "in_progress"},
            ],
        )
    ok = strict.write("s1", [{"content": "a", "status": "in_progress"}])
    assert ok["counts"]["inProgress"] == 1


def test_parallel_allowed_by_default(store):
    out = store.write(
        "s1",
        [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ],
    )
    assert out["counts"]["inProgress"] == 2


def test_sessions_are_isolated(store):
    store.write("s1", [{"content": "uno", "status": "pending"}])
    store.write("s2", [{"content": "dos", "status": "completed"}])
    assert store.read("s1")["todos"][0]["content"] == "uno"
    assert store.read("s2")["todos"][0]["content"] == "dos"


def test_clear_only_affects_target_session(store):
    store.write("s1", [{"content": "uno", "status": "pending"}])
    store.write("s2", [{"content": "dos", "status": "pending"}])
    store.clear("s1")
    assert store.read("s1")["todos"] == []
    assert len(store.read("s2")["todos"]) == 1


@pytest.mark.parametrize(
    "todos",
    [
        "no-es-lista",
        ["suelto"],
        [{"content": 123, "status": "pending"}],
        [{"content": "a", "status": 2}],
        [{"content": "a", "status": None}],
        [{"content": None, "status": "pending"}],
    ],
)
def test_forma_invalida_es_valueerror_no_attributeerror(store, todos):
    with pytest.raises(ValueError):
        store.write("s1", todos)
    assert store.read("s1")["todos"] == []
