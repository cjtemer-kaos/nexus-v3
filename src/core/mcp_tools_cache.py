"""
MCP Tools Cache — In-memory + disk cache for MCP probe results.
Adapted from Hermes mcp-tools-cache.ts.
"""

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CachedProbe:
    status: str  # connected | failed | unknown
    tool_count: int = 0
    tool_names: List[str] = field(default_factory=list)
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    tested_at: float = 0.0


_TTL = int(os.environ.get("MCP_TOOLS_CACHE_TTL", "86400"))  # 24h default
_probes: Dict[str, CachedProbe] = {}
_cache_dir = Path(os.environ.get("NEXUS_HOME", Path.home() / ".nexus")) / "cache"
_cache_path = _cache_dir / "mcp_tools.json"


def _load():
    if _probes:
        return
    try:
        if _cache_path.exists():
            data = json.loads(_cache_path.read_text(encoding="utf-8"))
            for name, p in data.get("probes", {}).items():
                _probes[name] = CachedProbe(**p)
    except Exception as e:
        logger.debug(f"MCP tools cache load: {e}")


def _save():
    try:
        _cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path.with_suffix(f".tmp.{os.getpid()}.{random.randint(0, 0xFFFFFF):06x}")
        tmp.write_text(json.dumps({
            "version": 1,
            "probes": {k: v.__dict__ for k, v in _probes.items()},
        }, indent=2), encoding="utf-8")
        tmp.replace(_cache_path)
    except Exception as e:
        logger.debug(f"MCP tools cache save: {e}")


def set_probe(name: str, status: str, tool_count: int = 0, tool_names: Optional[List[str]] = None,
              latency_ms: Optional[float] = None, error: Optional[str] = None):
    _load()
    _probes[name] = CachedProbe(
        status=status,
        tool_count=tool_count,
        tool_names=tool_names or [],
        latency_ms=latency_ms,
        error=error,
        tested_at=time.time(),
    )
    _save()


def get_probe(name: str) -> Optional[CachedProbe]:
    _load()
    p = _probes.get(name)
    if p is None:
        return None
    return p


def clear_probe(name: str):
    _load()
    _probes.pop(name, None)
    _save()


def list_probes() -> Dict[str, CachedProbe]:
    _load()
    return dict(_probes)
