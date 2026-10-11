"""Brain link — enlaza a un brain externo sin duplicar.

Si NEXUS_BRAIN apunta a otro directorio (ej. otra instalacion), Nexus v3
lee/escribe los mismos cerebro.db / nexus_memory.db / knowledge_graph.db.
Sin sync, sin duplicacion.

Ver BLUEPRINT.md §11
Patron heredado de src/bridges/mcp_bridge_server.py:17 en v2.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_BRAIN = Path(__file__).parents[2] / "brain"

# Known brain files
BRAIN_FILES = [
    "cerebro.db",
    "nexus_memory.db",
    "knowledge_graph.db",
    "message_board.db",
    "sessions.db",
    "event_store.db",
]


def _expand_brain_path(raw: str) -> Path:
    """Expand ${NEXUS_BRAIN:-./brain} style and env vars."""
    # Handle ${VAR:-default} syntax that may leak from config.yaml parsing
    m = re.match(r"\$\{([^:}]+):-([^}]+)\}", raw.strip())
    if m:
        var, default = m.group(1), m.group(2)
        val = os.environ.get(var, default)
        return Path(val).expanduser()
    # Handle $VAR or ${VAR}
    expanded = os.path.expandvars(raw)
    # Handle leading ~/
    return Path(expanded).expanduser()


def brain_path() -> Path:
    # nexus-v3 usa SU PROPIO brain. Un ``NEXUS_BRAIN`` ambiental puede pertenecer
    # a OTRO proyecto de la misma PC (p.ej. supernexus-v2) y enlazar cerebros por
    # accidente. Solo se honra un override v3-scoped (``NEXUS_V3_BRAIN``).
    raw = os.environ.get("NEXUS_V3_BRAIN") or str(DEFAULT_BRAIN)
    p = _expand_brain_path(raw)
    # If relative, resolve against project root (parents[2])
    if not p.is_absolute():
        p = (Path(__file__).parents[2] / p).resolve()
    return p


def cerebro_db() -> Path:
    return brain_path() / "cerebro.db"


def nexus_memory_db() -> Path:
    return brain_path() / "nexus_memory.db"


def knowledge_graph_db() -> Path:
    return brain_path() / "knowledge_graph.db"


def message_board_db() -> Path:
    return brain_path() / "message_board.db"


def event_store_db() -> Path:
    return brain_path() / "event_store.db"


def brain_exists() -> bool:
    """True if at least one brain DB exists."""
    bp = brain_path()
    return (bp / "cerebro.db").exists() or (bp / "nexus_memory.db").exists() or bp.exists()


def is_linked_to_v2() -> bool:
    """True if NEXUS_BRAIN points to an external brain with data."""
    bp = brain_path()
    # heuristic: external brain has a large cerebro.db (>1MB)
    cb = bp / "cerebro.db"
    if cb.exists() and cb.stat().st_size > 1_000_000:
        return True
    # also check if brain differs from local default
    if bp.resolve() != DEFAULT_BRAIN.resolve() and brain_exists():
        return True
    return False


def ensure_brain_dir() -> Path:
    bp = brain_path()
    bp.mkdir(parents=True, exist_ok=True)
    return bp


def list_brain_files() -> list[Path]:
    bp = brain_path()
    if not bp.exists():
        return []
    return [p for p in bp.iterdir() if p.is_file()]


def get_brain_info() -> dict[str, Any]:
    """Resumen del brain enlazado (para --check-config / diagnostics)."""
    bp = brain_path()
    exists = bp.exists()
    files: dict[str, Any] = {}
    total_bytes = 0
    if exists:
        for name in BRAIN_FILES:
            fp = bp / name
            if fp.exists():
                try:
                    sz = fp.stat().st_size
                except Exception:
                    sz = 0
                files[name] = {"exists": True, "bytes": sz, "path": str(fp)}
                total_bytes += sz
            else:
                files[name] = {"exists": False, "bytes": 0, "path": str(fp)}
        # also list any extra files
        try:
            extra = [p.name for p in bp.iterdir() if p.name not in BRAIN_FILES and p.is_file()]
        except Exception:
            extra = []
    else:
        for name in BRAIN_FILES:
            files[name] = {"exists": False, "bytes": 0, "path": str(bp / name)}
        extra = []

    return {
        "brain_path": str(bp),
        "exists": exists,
        "linked_to_v2": is_linked_to_v2(),
        "nexus_brain_env": os.environ.get("NEXUS_V3_BRAIN", ""),
        "default_brain": str(DEFAULT_BRAIN),
        "files": files,
        "extra_files": extra,
        "total_bytes": total_bytes,
        "brain_exists": brain_exists(),
    }


def connect_db(db_name: str, read_only: bool = False) -> sqlite3.Connection:
    """Open a brain DB with WAL-friendly pragmas."""
    bp = brain_path()
    db_path = bp / db_name
    if read_only:
        # sqlite URI mode=ro REQUIERE uri=True; as_posix() evita backslashes
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True,
                               check_same_thread=False, timeout=30.0)
    else:
        conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    return conn


def query_cerebro(sql: str, params: tuple = (), read_only: bool = True) -> list[sqlite3.Row]:
    """Convenience: query cerebro.db and return rows."""
    conn = connect_db("cerebro.db", read_only=read_only)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        return rows
    finally:
        try:
            conn.close()
        except Exception:
            pass


def check_brain_health() -> dict[str, Any]:
    """Probe each brain DB for readability."""
    bp = brain_path()
    health: dict[str, Any] = {}
    for name in BRAIN_FILES:
        fp = bp / name
        if not fp.exists():
            health[name] = {"exists": False, "readable": False}
            continue
        try:
            conn = sqlite3.connect(f"file:{fp.as_posix()}?mode=ro", uri=True, timeout=5.0)
            conn.execute("SELECT 1").fetchone()
            conn.close()
            health[name] = {"exists": True, "readable": True}
        except Exception as e:
            health[name] = {"exists": True, "readable": False, "error": str(e)}
    return health
