"""Sección [5] Brain link (paridad run_tests.py)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
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
