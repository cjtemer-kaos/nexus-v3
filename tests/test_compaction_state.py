"""Estado de compactacion durable (metricas + fallback de resumen)."""
from __future__ import annotations

from src.core.agent_loop import AgentLoop
from src.core.session_log import SessionLog


class FakeMetrics:
    def to_dict(self) -> dict:
        return {
            "original_tokens": 1000,
            "compressed_tokens": 250,
            "tokens_saved": 750,
            "layers_applied": ["snip", "summary"],
        }


def _log(tmp_path) -> SessionLog:
    log = SessionLog(str(tmp_path / "session.db"))
    log.create_session(project="test", session_id="s1")
    return log


def test_save_and_get_compaction_state(tmp_path):
    log = _log(tmp_path)
    st = log.save_compaction_state(
        "s1", tokens_before=1000, tokens_after=250, saved=750,
        layers=["snip", "summary"], actions=2, summary="[resumen de x] y",
    )
    assert st["count"] == 1
    assert st["tokens_before"] == 1000
    assert st["tokens_after"] == 250
    assert st["saved"] == 750
    assert st["layers"] == ["snip", "summary"]
    assert st["summary"].startswith("[resumen de x]")

    again = log.save_compaction_state(
        "s1", tokens_before=800, tokens_after=200, saved=600,
        layers=["summary"], actions=1,
    )
    assert again["count"] == 2
    assert again["tokens_before"] == 800
    # el resumen previo se conserva si el nuevo viene vacio
    assert again["summary"].startswith("[resumen de x]")
    assert log.get_compaction_state("nope") is None


def test_agent_loop_persists_metrics_and_summary(tmp_path):
    log = _log(tmp_path)
    loop = AgentLoop(session_log=log)
    loop._persist_compaction_state(
        "s1", FakeMetrics(),
        [{"role": "system", "content": "[resumen de contexto] quedamos en X"}],
    )
    st = log.get_compaction_state("s1")
    assert st is not None
    assert st["tokens_before"] == 1000
    assert st["tokens_after"] == 250
    assert st["saved"] == 750
    assert st["layers"] == ["snip", "summary"]
    assert st["summary"] == "[resumen de contexto] quedamos en X"
    # compact_count de la sesion no se toca aca: lo maneja compact_mark
    assert log.get_session("s1") is not None
