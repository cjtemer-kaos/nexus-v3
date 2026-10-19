"""Sección [5] Brain link (paridad run_tests.py)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from src.brain import link
from src.brain.link import (
    DEFAULT_BRAIN,
    brain_exists,
    brain_path,
    check_brain_health,
    get_brain_info,
    is_linked_to_v2,
    list_brain_files,
)


@pytest.fixture
def no_nexus_brain():
    """Simula el pop de NEXUS_V3_BRAIN del run_tests (linea 577) y restaura."""
    orig = os.environ.pop("NEXUS_V3_BRAIN", None)
    yield
    if orig is not None:
        os.environ["NEXUS_V3_BRAIN"] = orig


def test_brain_path_default():
    bp = brain_path()
    assert bp


def test_get_brain_info_shape():
    bi = get_brain_info()
    assert "exists" in bi or "brain_exists" in bi


def test_brain_exists():
    brain_exists()


def test_is_linked_to_v2():
    linked = is_linked_to_v2()
    assert isinstance(linked, bool)


def test_nexus_brain_env_override(tmp_path):
    orig = os.environ.get("NEXUS_V3_BRAIN")
    try:
        (Path(tmp_path) / "cerebro.db").write_bytes(b"\x00" * 1024)
        (Path(tmp_path) / "nexus_memory.db").write_bytes(b"\x00" * 512)
        os.environ["NEXUS_V3_BRAIN"] = str(tmp_path)
        bp2 = brain_path()
        assert str(bp2) == str(tmp_path) or str(bp2) == Path(tmp_path).resolve().as_posix()
        bi2 = get_brain_info()
        assert bi2["exists"] is True
        assert bi2["files"]["cerebro.db"]["exists"] is True
        files = list_brain_files()
        assert len(files) == 2
        health = check_brain_health()
        assert "cerebro.db" in health
        health.get("cerebro.db", {}).get("readable")  # archivos fake: solo informativo (paridad run_tests)
    finally:
        if orig is None:
            os.environ.pop("NEXUS_V3_BRAIN", None)
        else:
            os.environ["NEXUS_V3_BRAIN"] = orig


def test_config_path_expansion(no_nexus_brain):
    from src.core.config import load_config

    cfg = load_config()
    raw_path = cfg.get("brain", {}).get("path")
    # config.yaml usa ${NEXUS_V3_BRAIN:-./brain}
    assert raw_path is not None
    brain_path()


def test_check_config_brain_ok():
    from src.core.config import check_config

    cc = check_config()
    assert "brain_ok" in cc and "valid" in cc


def test_default_brain_importable():
    assert DEFAULT_BRAIN is not None


# --- Precedencia y portabilidad (env > config > ./brain) ---------------------


@pytest.fixture(autouse=True)
def _brain_link_clean(monkeypatch):
    monkeypatch.delenv("NEXUS_V3_BRAIN", raising=False)
    link._cfg_brain_cache.update(mtime=None, raw="")
    yield


def test_env_has_priority(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("brain:\n  path: ./desde-config\n", encoding="utf-8")
    monkeypatch.setattr(link, "_CONFIG_FILE", cfg)
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path / "desde-env"))
    assert link.brain_path() == (tmp_path / "desde-env")


def test_config_path_is_honoured(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"brain:\n  path: '{tmp_path / 'cerebro-propio'}'\n", encoding="utf-8")
    monkeypatch.setattr(link, "_CONFIG_FILE", cfg)
    assert link.brain_path() == tmp_path / "cerebro-propio"


def test_env_expansion_syntax(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("brain:\n  path: ${NEXUS_V3_BRAIN:-./brain}\n", encoding="utf-8")
    monkeypatch.setattr(link, "_CONFIG_FILE", cfg)
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path / "expandido"))
    assert link.brain_path() == tmp_path / "expandido"


def test_relative_config_resolves_against_project_root(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("brain:\n  path: ./brain-local\n", encoding="utf-8")
    monkeypatch.setattr(link, "_CONFIG_FILE", cfg)
    p = link.brain_path()
    assert p.is_absolute()
    assert p.name == "brain-local"
    assert p.parent == Path(link.__file__).parents[2]


def test_missing_config_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setattr(link, "_CONFIG_FILE", tmp_path / "no-existe.yaml")
    assert link.brain_path() == link.DEFAULT_BRAIN


def test_broken_config_falls_back_to_default(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("brain: [no, es, un, dict]\n", encoding="utf-8")
    monkeypatch.setattr(link, "_CONFIG_FILE", cfg)
    assert link.brain_path() == link.DEFAULT_BRAIN


def test_config_cache_invalidates_on_change(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("brain:\n  path: ./uno\n", encoding="utf-8")
    monkeypatch.setattr(link, "_CONFIG_FILE", cfg)
    assert link.brain_path().name == "uno"

    import time

    cfg.write_text("brain:\n  path: ./dos\n", encoding="utf-8")
    # mtime con resolucion de ns en Windows; forzamos un salto por si el FS agrupa.
    st = cfg.stat()
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    time.sleep(0.01)
    assert link.brain_path().name == "dos"


def test_nexus_brain_env_is_ignored(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"brain:\n  path: '{tmp_path / 'propio'}'\n", encoding="utf-8")
    monkeypatch.setattr(link, "_CONFIG_FILE", cfg)
    monkeypatch.setenv("NEXUS_BRAIN", str(tmp_path / "brain-de-v2"))
    assert link.brain_path() == tmp_path / "propio"


def test_ensure_local_brain_exports_resolved_path(tmp_path, monkeypatch):
    from src.api.server import _ensure_local_brain

    target = tmp_path / "brain-de-test"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"brain:\n  path: '{target.as_posix()}'\n", encoding="utf-8")
    monkeypatch.setattr(link, "_CONFIG_FILE", cfg)
    monkeypatch.delenv("NEXUS_V3_BRAIN", raising=False)

    out = _ensure_local_brain()
    assert out == str(target)
    assert os.environ["NEXUS_V3_BRAIN"] == out
