"""Tests de BoundedHistory: tope, TTL y coherencia entre len() y get_all()."""
import time

from src.core.bounded_history import BoundedHistory


def test_maxlen_evicts_oldest():
    h = BoundedHistory(maxlen=3)
    h.extend([1, 2, 3, 4, 5])
    assert h.get_all() == [3, 4, 5]
    assert h.maxlen == 3


def test_get_recent_returns_tail():
    h = BoundedHistory(maxlen=10)
    h.extend([1, 2, 3])
    assert h.get_recent(2) == [2, 3]
    assert h.get_recent(99) == [1, 2, 3]


def test_ttl_filters_expired_items():
    h = BoundedHistory(maxlen=10, ttl_seconds=0.05)
    h.append("viejo")
    time.sleep(0.07)
    assert h.get_all() == []
    assert len(h) == 0, "len() no debe contar items expirados"


def test_ttl_keeps_fresh_items():
    h = BoundedHistory(maxlen=10, ttl_seconds=5)
    h.append("fresco")
    assert h.get_all() == ["fresco"]
    assert len(h) == 1


def test_iter_and_clear():
    h = BoundedHistory(maxlen=5)
    h.extend(["a", "b"])
    assert list(h) == ["a", "b"]
    h.clear()
    assert list(h) == [] and len(h) == 0
