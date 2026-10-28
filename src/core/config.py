"""Config loader — unica fuente de verdad (config.yaml + env + schema).

Loads config/config.yaml, expands ${VAR} and ${VAR:-default}, validates against config.schema.json.
"""
from __future__ import annotations

import os
import re
import json
from pathlib import Path
from typing import Any

try:
    import yaml  # PyYAML
except ImportError:
    yaml = None  # fallback to json-like parsing

CONFIG_PATH = Path(__file__).parents[2] / "config" / "config.yaml"
SCHEMA_PATH = Path(__file__).parents[2] / "config" / "config.schema.json"

_ENV_VAR_RE = re.compile(r"\$\{([^:}]+)(?::-(.*?))?\}")
_SIMPLE_ENV_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
# Un valor con estos caracteres es una regex (o al menos no es una ruta): no se
# le expanden los `$VAR` sueltos. Ver _expand_env.
_REGEXISH_RE = re.compile(r"[\\()\[\]|?^*+]")


def _expand_env(value: str) -> str:
    if not isinstance(value, str) or "$" not in value:
        return value
    # ${VAR:-default} and ${VAR}
    def repl(m):
        var = m.group(1)
        default = m.group(2) if m.group(2) is not None else ""
        return os.environ.get(var, default)
    value = _ENV_VAR_RE.sub(repl, value)
    # $VAR suelto: comodo para rutas, pero NUNCA dentro de una regex. La regla
    # deny-rm-root tiene `\$home`; con HOME definido (Windows lo define) se
    # expandia a `\C:\Users\...`, el re.compile fallaba y el guard se quedaba
    # sin el deny de `rm -rf ~` sin que nadie se enterara.
    if "$" in value and not _REGEXISH_RE.search(value):
        value = _SIMPLE_ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    return value


def _expand_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _expand_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_obj(v) for v in obj]
    return obj


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    if yaml is None:
        # Fallback: try json, else simple parse
        text = p.read_text(encoding="utf-8")
        # Very naive YAML fallback: only works if yaml is JSON-compatible
        try:
            data = json.loads(text)
        except Exception:
            raise ImportError("PyYAML required to load config.yaml (pip install pyyaml)")
    else:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data = _expand_obj(data)
    return data


def validate_config(data: dict, schema_path: str | Path | None = None) -> list[str]:
    """Validate against JSON Schema. Returns list of errors (empty if ok)."""
    sp = Path(schema_path) if schema_path else SCHEMA_PATH
    if not sp.exists():
        return []
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return []  # skip validation if not installed
    schema = json.loads(sp.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)  # type: ignore
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors]


def load_raw_config(path: str | Path | None = None) -> dict:
    """Load YAML without env expansion — for editing/saving."""
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    if yaml is None:
        raise ImportError("PyYAML required (pip install pyyaml)")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _apply_dict(doc: dict, data: dict) -> None:
    """Actualiza doc in-place con data (recursivo), preservando comentarios/orden
    de las claves existentes. Elimina claves que ya no estan en data."""
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(doc.get(k), dict):
            _apply_dict(doc[k], v)
        else:
            doc[k] = v
    for k in list(doc.keys()):
        if k not in data:
            del doc[k]


def save_config(data: dict, path: str | Path | None = None) -> None:
    """Persist config dict to YAML PRESERVANDO comentarios (ruamel round-trip) si
    esta disponible; fallback a PyYAML (pierde comentarios)."""
    p = Path(path) if path else CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        from ruamel.yaml import YAML
        yml = YAML()
        yml.preserve_quotes = True
        yml.width = 4096
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                doc = yml.load(f) or {}
        else:
            doc = {}
        _apply_dict(doc, data)
        with p.open("w", encoding="utf-8") as f:
            yml.dump(doc, f)
        return
    except Exception:
        pass
    if yaml is None:
        raise ImportError("PyYAML required (pip install pyyaml)")
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    p.write_text(text, encoding="utf-8")


def check_config(path: str | Path | None = None) -> dict:
    """Load + validate + brain_path check. Returns diagnostic dict."""
    cfg = load_config(path)
    errors = validate_config(cfg)
    # Brain check
    try:
        from src.brain.link import get_brain_info, brain_path
        brain_info = get_brain_info()
        brain_ok = brain_info.get("exists") or brain_info.get("brain_exists")
    except Exception as e:
        brain_info = {"error": str(e)}
        brain_ok = False
    return {
        "config_path": str(path or CONFIG_PATH),
        "project": cfg.get("project"),
        "valid": len(errors) == 0,
        "errors": errors,
        "brain": brain_info,
        "brain_ok": brain_ok,
        "session": cfg.get("session"),
        "providers": list(cfg.get("providers", {}).keys()),
        "router": cfg.get("router"),
        "scope": cfg.get("scope"),
    }
