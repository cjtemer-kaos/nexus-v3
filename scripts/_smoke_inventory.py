"""Inventario del registry real (el mismo que usa el chat)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.server import _build_registry, _build_router_from_config, _build_scope, _load_config_safe  # noqa: E402


def main() -> int:
    cfg = _load_config_safe()
    scope = _build_scope(cfg)
    router = _build_router_from_config(cfg)
    reg = _build_registry(scope, "smoke-tools", cfg, router=router)
    filas = []
    for e in sorted(reg.list(), key=lambda x: x.name):
        params = (e.parameters or {}).get("properties", {}) or {}
        filas.append({
            "name": e.name,
            "category": getattr(e, "category", ""),
            "required": (e.parameters or {}).get("required", []),
            "params": {k: (v or {}).get("type", "?") for k, v in params.items()},
            "desc": (e.description or "").split("\n")[0][:100],
        })
    print(json.dumps({"count": len(filas), "tools": filas}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
