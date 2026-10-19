"""El config.yaml real (y las claves que el server lee) deben pasar el schema.

`additionalProperties: false` hace que una clave usada por el código pero ausente
del schema bloquee cualquier PUT /api/config: la UI no puede guardar ajustes.
"""
import copy
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "config" / "config.schema.json").read_text(encoding="utf-8"))
CFG = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))


def _validate(cfg):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(cfg, SCHEMA)


def test_config_yaml_matches_schema():
    _validate(CFG)


def test_keys_read_by_server_are_declared():
    cfg = copy.deepcopy(CFG)
    cfg.setdefault("session", {})["event_db_path"] = "./data/event_store.db"
    cfg.setdefault("scope", {})["require_confirm_mutating"] = True
    loop = cfg.setdefault("agent_loop", {})
    loop["auto_apply"] = "approve"
    loop["budget"] = {
        "max_iterations": 25,
        "max_tokens": 32000,
        "max_wall_ms": 900000,
        "max_tool_calls": 60,
    }
    _validate(cfg)


def test_unknown_key_is_rejected():
    cfg = copy.deepcopy(CFG)
    cfg["agent_loop"]["clave_inventada"] = 1
    with pytest.raises(Exception):
        _validate(cfg)
