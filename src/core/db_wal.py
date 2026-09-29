"""
WAL Fallback - SQLite WAL mode with graceful degradation.
Absorbed from hermes-agent — names cleaned.
"""

import logging
import sqlite3
import time
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

_WAL_INCOMPAT_MARKERS = (
    "locking protocol",
    "not authorized",
    "disk i/o error",
)

_wal_fallback_warned = set()


def db_connection(db_path: Union[str, Path], *, db_label: str = "db") -> sqlite3.Connection:
    """Factory: abre conexion SQLite con WAL fallback y pragmas estandar.

    Reemplaza `sqlite3.connect()` directo en archivos absorbed/centrales.
    Para archivos legado usar open_db() directamente.
    """
    return open_db(Path(db_path), db_label=db_label)


def apply_wal_with_fallback(conn: sqlite3.Connection, *, db_label: str = "db") -> str:
    """Apply WAL journal mode, falling back to DELETE on NFS/SMB/FUSE incompatibility.
    Returns 'wal' or 'delete'."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        return "wal"
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if not any(marker in msg for marker in _WAL_INCOMPAT_MARKERS):
            raise
        if db_label not in _wal_fallback_warned:
            _wal_fallback_warned.add(db_label)
            logger.warning(f"WAL not supported for {db_label}, falling back to DELETE: {exc}")
        conn.execute("PRAGMA journal_mode=DELETE")
        return "delete"


def safe_checkpoint(conn: sqlite3.Connection, mode: str = "PASSIVE") -> bool:
    """Safe WAL checkpoint — no-op if not in WAL mode."""
    try:
        result = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        return result[0] == 0
    except Exception:
        return False


def open_db(db_path: Path, *, db_label: str = "db") -> sqlite3.Connection:
    """Open SQLite DB with WAL fallback and common pragmas."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    apply_wal_with_fallback(conn, db_label=db_label)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def begin_immediate(conn: sqlite3.Connection, *, max_retries: int = 15) -> bool:
    """BEGIN IMMEDIATE with jitter retry for write contention."""
    import random
    for attempt in range(max_retries):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return True
        except sqlite3.OperationalError:
            time.sleep(random.uniform(0.02, 0.15))
    return False
