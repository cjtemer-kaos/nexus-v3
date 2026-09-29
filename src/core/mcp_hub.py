"""
MCP Hub — Unified search with multi-source aggregation.
Adapted from Hermes MCP Hub (index.ts, cache.ts, trust.ts).

Aggregates MCP server entries from multiple sources in parallel,
deduplicates by source+id+name, marks installed entries, falls
back to local file when all remote sources fail.
"""

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class McpEntry:
    source: str
    id: str
    name: str
    description: str = ""
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"
    url: Optional[str] = None
    trust: str = "unverified"  # official | community | unverified
    installed: bool = False
    tags: List[str] = field(default_factory=list)


@dataclass
class SourceResult:
    entries: List[McpEntry]
    source_label: str
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Source adapter interface
# ---------------------------------------------------------------------------

class McpSource:
    async def fetch(self, signal: asyncio.Event) -> SourceResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Cache — two-tier memory + disk
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    payload: list
    fetched_at: float
    expires_at: float


_MEM_CACHE: Dict[str, CacheEntry] = {}
_MEM_TTL = 1800       # 30 min
_DISK_TTL = 86400     # 24 h
_CACHE_DIR = Path(os.environ.get("NEXUS_HOME", Path.home() / ".nexus")) / "cache" / "mcp_hub"


def _cache_path(source: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in source)
    return _CACHE_DIR / f"{safe}.json"


def _read_cache(source: str) -> Optional[list]:
    now = time.monotonic()
    mem = _MEM_CACHE.get(source)
    if mem and now < mem.expires_at:
        return mem.payload

    path = _cache_path(source)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            expires_disk = data.get("expires_at_disk", 0)
            if data.get("fetched_at", 0) + _DISK_TTL < time.time():
                return None
            payload = data.get("payload", [])
            _MEM_CACHE[source] = CacheEntry(
                payload=payload,
                fetched_at=data.get("fetched_at", 0),
                expires_at=now + _MEM_TTL,
            )
            return payload
        except Exception:
            return None
    return None


def _write_cache(source: str, payload: list):
    now_m = time.monotonic()
    now_w = time.time()
    _MEM_CACHE[source] = CacheEntry(payload=payload, fetched_at=now_w, expires_at=now_m + _MEM_TTL)
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(source)
        tmp = path.with_suffix(f".tmp.{os.getpid()}.{random.randint(0, 0xFFFFFF):06x}")
        tmp.write_text(json.dumps({
            "payload": payload,
            "fetched_at": now_w,
            "expires_at_disk": now_w + _DISK_TTL,
        }, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.debug(f"MCP hub cache write failed: {e}")


# ---------------------------------------------------------------------------
# Trust helpers
# ---------------------------------------------------------------------------

TRUST_ORDER = {"official": 0, "community": 1, "unverified": 2}


def trust_score(trust: str) -> int:
    return TRUST_ORDER.get(trust, 99)


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def deduplicate(entries: List[McpEntry]) -> List[McpEntry]:
    seen: Set[str] = set()
    result = []
    for e in entries:
        key = f"{e.source}:{e.id}:{e.name}"
        if key not in seen:
            seen.add(key)
            result.append(e)
    return result


# ---------------------------------------------------------------------------
# Unified search — main entry point
# ---------------------------------------------------------------------------

async def unified_search(
    query: str = "",
    installed_names: Optional[Set[str]] = None,
    sources: Optional[List[McpSource]] = None,
    per_source_timeout: float = 8.0,
) -> Tuple[List[McpEntry], List[str]]:
    if sources is None:
        sources = _default_sources()
    if installed_names is None:
        installed_names = set()

    async def _fetch_one(src: McpSource) -> SourceResult:
        try:
            done_event = asyncio.Event()
            task = asyncio.create_task(src.fetch(done_event))
            result = await asyncio.wait_for(task, timeout=per_source_timeout)
            return result
        except asyncio.TimeoutError:
            return SourceResult(entries=[], source_label=getattr(src, "_label", "unknown"), degraded=True, warnings=["timeout"])
        except Exception as e:
            return SourceResult(entries=[], source_label=getattr(src, "_label", "unknown"), degraded=True, warnings=[str(e)])

    settled = await asyncio.gather(*[_fetch_one(s) for s in sources], return_exceptions=True)

    all_entries: List[McpEntry] = []
    warnings: List[str] = []
    any_remote_ok = False

    for res in settled:
        if isinstance(res, Exception):
            warnings.append(f"Source error: {res}")
            continue
        if res.degraded:
            warnings.extend(res.warnings)
        else:
            any_remote_ok = True
        for e in res.entries:
            e.installed = e.name in installed_names
        all_entries.extend(res.entries)

    if not any_remote_ok:
        for src in sources:
            label = getattr(src, "_label", "?")
            if label == "local":
                res = await _fetch_one(src)
                all_entries.extend(res.entries)

    all_entries = deduplicate(all_entries)

    if query:
        q = query.lower()
        all_entries = [
            e for e in all_entries
            if q in e.name.lower() or q in e.description.lower() or any(q in t.lower() for t in e.tags)
        ]

    return all_entries, warnings


# ---------------------------------------------------------------------------
# Default sources
# ---------------------------------------------------------------------------

class LocalFileSource(McpSource):
    _label = "local"

    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path.home() / ".nexus" / "mcp_presets.json"

    async def fetch(self, signal: asyncio.Event) -> SourceResult:
        return self._read_local()

    def _read_local(self) -> SourceResult:
        if not self.path.exists():
            return SourceResult(entries=[], source_label="local")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = []
            for name, cfg in data.items():
                entries.append(McpEntry(
                    source="local",
                    id=f"local:{name}",
                    name=name,
                    description=cfg.get("description", ""),
                    command=cfg.get("command"),
                    args=cfg.get("args", []),
                    env=cfg.get("env", {}),
                    transport=cfg.get("transport", "stdio"),
                    url=cfg.get("url"),
                    trust="official",
                ))
            return SourceResult(entries=entries, source_label="local")
        except Exception as e:
            return SourceResult(entries=[], source_label="local", degraded=True, warnings=[str(e)])


class StaticMcpSource(McpSource):
    def __init__(self, label: str, entries: List[McpEntry], trust: str = "community"):
        self._label = label
        self._entries = entries
        self._trust = trust

    async def fetch(self, signal: asyncio.Event) -> SourceResult:
        for e in self._entries:
            e.source = self._label
        return SourceResult(entries=self._entries, source_label=self._label)


def _default_sources() -> List[McpSource]:
    return [LocalFileSource()]
