"""Tests de la politica de errores: clasificacion, decision y reintentos."""
import pytest

from src.core.error_envelope import (
    ErrorCategory,
    ErrorClassifier,
    ErrorDecision,
    ErrorEnvelope,
    ErrorSeverity,
    RetryManager,
)


def test_security_errors_are_critical_and_never_retried():
    env = ErrorClassifier.classify(Exception("SSRF attempt blocked"))
    assert env.category is ErrorCategory.SECURITY
    assert env.severity is ErrorSeverity.CRITICAL
    assert env.is_retryable is False
    assert env.max_retries == 0
    assert env.decision is ErrorDecision.ABORT
    assert env.should_retry() is False


def test_timeout_is_network_and_retryable():
    env = ErrorClassifier.classify(TimeoutError("connection timed out"))
    assert env.category is ErrorCategory.NETWORK
    assert env.is_retryable is True
    assert env.max_retries == 5
    assert env.decision is ErrorDecision.RETRY
    assert env.fix_suggestion


def test_filesystem_error_is_classified():
    env = ErrorClassifier.classify(PermissionError("permission denied: /x"))
    assert env.category is ErrorCategory.FILESYSTEM


def test_backoff_grows_exponentially():
    env = ErrorClassifier.classify(TimeoutError("timeout"))
    d0 = env.get_retry_delay()
    env.record_retry()
    assert env.get_retry_delay() == d0 * 2
    env.record_retry()
    assert env.get_retry_delay() == d0 * 4


def test_retry_budget_is_exhausted():
    env = ErrorClassifier.classify(TimeoutError("timeout"))
    n = 0
    while env.should_retry():
        env.record_retry()
        n += 1
        assert n <= env.max_retries + 1
    assert n == env.max_retries


def test_to_dict_is_jsonable():
    import json
    env = ErrorClassifier.classify(TimeoutError("timeout"), context={"attempt": 1})
    d = env.to_dict()
    json.dumps(d)
    assert d["category"] == "network"
    assert d["context"] == {"attempt": 1}
    assert d["message"] == "timeout"


def test_envelope_defaults_message_from_error():
    env = ErrorEnvelope(error=ValueError("boom"))
    assert env.message == "boom"


def test_retry_manager_retries_until_success():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("connection timeout")
        return "ok"

    rm = RetryManager(max_retries=3, base_delay=0)
    assert rm.execute_with_retry(flaky) == "ok"
    assert calls["n"] == 3
    assert len(rm.history) == 2


def test_retry_manager_does_not_retry_security_errors():
    calls = {"n": 0}

    def blocked():
        calls["n"] += 1
        raise Exception("dangerous command blocked")

    rm = RetryManager(max_retries=3, base_delay=0)
    with pytest.raises(Exception, match="blocked"):
        rm.execute_with_retry(blocked)
    assert calls["n"] == 1
    assert rm.history[0].decision is ErrorDecision.ABORT


def test_retry_manager_summary():
    def boom():
        raise TimeoutError("timeout")

    rm = RetryManager(max_retries=1, base_delay=0)
    with pytest.raises(TimeoutError):
        rm.execute_with_retry(boom)
    s = rm.get_error_summary()
    assert s["total_errors"] == len(rm.history) >= 1
    assert "network" in s["by_category"]
