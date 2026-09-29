"""MCP server autodiscovery — load MCP servers from JSON files.

Convention: any directory can contain `mcp_servers.json`. The loader scans
a priority-ordered list of paths and merges discovered servers. Duplicate
names are merged (first wins). Corrupt JSON is skipped with a log error.

To add a server without touching code: drop an `mcp_servers.json` in one
of the scanned paths. See docs/MCP_SERVERS.md for the full reference.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SCAN_PATHS: List[Path] = [
    Path("mcp_servers.json"),
    Path("src/bridges/mcp_servers.json"),
    Path.home() / ".nexus" / "mcp_servers.json",
]


def discover_servers(
    scan_paths: Optional[List[Path]] = None,
) -> List[Dict]:
    servers: List[Dict] = []
    seen: set = set()
    paths = scan_paths or SCAN_PATHS

    for path in paths:
        rp = path.resolve()
        if not rp.is_file():
            continue
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            logger.error(f"mcp_autodiscovery: corrupt {rp} — {e}")
            continue
        for srv in data.get("servers", []):
            name = srv.get("name", "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            srv["_source"] = str(rp)
            servers.append(srv)

    logger.info(
        f"mcp_autodiscovery: {len(servers)} server(s) from "
        f"{sum(1 for p in paths if p.resolve().is_file())} file(s)"
    )
    return servers


def discover_one(name: str) -> Optional[Dict]:
    """Return the first discovered server config matching `name`."""
    for srv in discover_servers():
        if srv.get("name") == name:
            return srv
    return None
