"""Thompson: unificar el tag `:latest` de Ollama con el id que registra el router."""
import json

from src.core import adaptive_router as ar


def _sampler(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "_ROUTER_STORAGE", tmp_path / "router_state.json")
    return ar.ThompsonSampler(catalog={})


def test_record_unifica_alias_latest(tmp_path, monkeypatch):
    s = _sampler(tmp_path, monkeypatch)
    s.record_outcome("qwen3.5-9b", 0.8)
    s.record_outcome("qwen3.5-9b:latest", 0.8)

    stats = s.get_stats()
    assert "qwen3.5-9b:latest" not in stats
    assert stats["qwen3.5-9b"]["calls"] == 2


def test_score_y_mean_mapean_alias(tmp_path, monkeypatch):
    s = _sampler(tmp_path, monkeypatch)
    s.record_outcome("qwen3.5-9b", 0.8)

    assert s.mean("qwen3.5-9b:latest") == s.mean("qwen3.5-9b")
    assert 0.0 < s.score("qwen3.5-9b:latest") < 1.0


def test_load_fusiona_alias_historico(tmp_path, monkeypatch):
    p = tmp_path / "router_state.json"
    p.write_text(json.dumps({"outcomes": {
        "qwen3.5-9b": {"alpha": 5.0, "beta": 2.0, "total_calls": 4, "success_count": 3},
        "qwen3.5-9b:latest": {"alpha": 2.0, "beta": 1.0, "total_calls": 1, "success_count": 1},
    }}), encoding="utf-8")
    monkeypatch.setattr(ar, "_ROUTER_STORAGE", p)

    s = ar.ThompsonSampler(catalog={})
    stats = s.get_stats()
    assert set(stats) == {"qwen3.5-9b"}
    assert stats["qwen3.5-9b"]["calls"] == 5
    # alpha = 1 (prior) + (5-1) + (2-1) = 6; el prior no se duplica
    assert stats["qwen3.5-9b"]["alpha"] == 6.0
