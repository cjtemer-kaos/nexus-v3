"""Sección [6] Config validation (paridad run_tests.py)."""
from __future__ import annotations

import os

import pytest
from src.core.config import CONFIG_PATH, SCHEMA_PATH, check_config, load_config, validate_config


def test_load_config():
    cfg = load_config()
    assert cfg.get("project")
    assert len(cfg.keys()) > 0


def test_validate_config_valid():
    cfg = load_config()
    errors = validate_config(cfg)
    assert len(errors) == 0


def test_check_config():
    cc = check_config()
    assert "valid" in cc and "providers" in cc and "router" in cc


def test_env_expansion(tmp_path):
    orig = os.environ.get("NEXUS_V3_BRAIN")
    try:
        os.environ["NEXUS_V3_BRAIN"] = "/tmp/test_brain_expansion"
        cfg_exp = load_config()
        assert "/tmp" in str(cfg_exp.get("brain", {}).get("path"))
    finally:
        if orig is None:
            os.environ.pop("NEXUS_V3_BRAIN", None)
        else:
            os.environ["NEXUS_V3_BRAIN"] = orig


def test_validate_config_invalid_missing_db_path():
    cfg = load_config()
    bad = dict(cfg)
    bad["session"] = {}
    bad_errors = validate_config(bad)
    assert len(bad_errors) > 0


def test_schema_file_exists():
    assert SCHEMA_PATH.exists()


@pytest.mark.parametrize(
    "section", ["brain", "session", "agent_loop", "scope", "compaction", "providers", "router", "hooks"]
)
def test_config_sections(section):
    cfg = load_config()
    assert section in cfg


def test_config_to_compaction_pipeline():
    from src.core.compaction import CompactionPipeline

    cfg = load_config()
    pipe = CompactionPipeline(cfg.get("compaction", {}))
    assert pipe.keep_recent is not None
    assert pipe.max_tool_result_chars is not None
