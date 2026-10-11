"""Session Store con version + handle (S1-fase1).

Capa fina sobre el SessionLog existente (backend read/append). NO cambia su
schema: el versionado vive en una tabla lateral `session_format`
(CREATE TABLE IF NOT EXISTS) y el single-writer es un registro en memoria.

Semantica portada de upstream deepseek-harness session-persistence:
- SESSION_FORMAT_VERSION = 0, rechazo direccional si mismatch, SIN migraciones.
- open/create/stat/list + SessionHandle read/append/flush/close.
- Single-writer: doble open con write RECHAZA (SessionAlreadyOwnedError);
  lecturas concurrentes siempre OK.
- flush = barrera de durabilidad: commit + fsync.
- stat.revision = "COUNT:MAX(created_at)" (token opaco de cambio).
- Crash-recovery: repair de turnos abiertos (turn/start sin turn/end) SOLO en
  memoria, sin truncar ni borrar nada (este backend SQLite no tiene cola
  fisica rasgada; no hay truncado).
- Errores: NotFound / AlreadyOwned / FormatUnsupported / Corruption
  (+ AlreadyExists en create duplicado, + ReadOnly en mutacion por handle
  read, como upstream).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from src.core.session_log import Message, SessionLog

SESSION_FORMAT_VERSION = 0

_FORMAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_format (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
class SessionNotFoundError(LookupError):
    def __init__(self, session_id: str):
        super().__init__(f'session "{session_id}" not found')
        self.session_id = session_id


class SessionAlreadyExistsError(ValueError):
    def __init__(self, session_id: str):
        super().__init__(f'session "{session_id}" already exists')
        self.session_id = session_id


class SessionAlreadyOwnedError(RuntimeError):
    def __init__(self, session_id: str):
        super().__init__(f'session "{session_id}" is already owned by an active write handle')
        self.session_id = session_id


class SessionFormatUnsupportedError(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)


class SessionCorruptionError(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)


class SessionReadOnlyError(RuntimeError):
    def __init__(self, session_id: str, operation: str):
        super().__init__(f'session "{session_id}": {operation} is not available on a read handle')
        self.session_id = session_id


class SessionHandleClosedError(RuntimeError):
    def __init__(self, session_id: str, operation: str):
        super().__init__(f'session "{session_id}": {operation} on a closed handle')
        self.session_id = session_id


def _version_refusal(session_id: str, version: int) -> str:
    if version > SESSION_FORMAT_VERSION:
        return (
            f'session "{session_id}" uses log format v{version}, but this harness reads only '
            f"v{SESSION_FORMAT_VERSION}: the log was written by a newer harness — upgrade the harness to open it"
        )
    return (
        f'session "{session_id}" uses log format v{version}, older than the supported '
        f"v{SESSION_FORMAT_VERSION}, and this build ships no upgrade path for it"
    )


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------
SessionAccess = Literal["read", "write"]

#: Marcadores de turno: un turno abre con tool_name="turn:start" y cierra con
#: tool_name="turn:end" (role "tool"). Convencion interna de esta fase.
TURN_START = "turn:start"
TURN_END = "turn:end"


@dataclass(frozen=True, slots=True)
class SessionHeader:
    id: str
    version: int = SESSION_FORMAT_VERSION
    project: str = "default"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {"id": self.id, "version": self.version, "project": self.project, "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    header: SessionHeader
    revision: str
    event_count: int


@dataclass(frozen=True, slots=True)
class RepairReport:
    repaired: bool
    open_turns: int


# ---------------------------------------------------------------------------
# Handle
# ---------------------------------------------------------------------------
class SessionHandle:
    """Un canal abierto sobre el log de una sesion (read-only o write)."""

    def __init__(self, store: SessionStore, header: SessionHeader, access: SessionAccess):
        self._store = store
        self._header = header
        self._access = access
        self._closed = False
        self._lock = threading.RLock()

    @property
    def id(self) -> str:
        return self._header.id

    @property
    def header(self) -> SessionHeader:
        return self._header

    @property
    def access(self) -> SessionAccess:
        return self._access

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _ensure_open(self, operation: str) -> None:
        if self._closed:
            raise SessionHandleClosedError(self._header.id, operation)

    def read(self, offset: int = 0, length: Optional[int] = None) -> list[Message]:
        with self._lock:
            self._ensure_open("read")
            if offset < 0:
                raise SessionCorruptionError(f"invalid read offset {offset} for session {self._header.id!r}")
            msgs = self._store._log.get_messages(self._header.id, limit=(length if length is not None else 10_000_000), offset=offset)
            return msgs

    def append(self, events: list[Message]) -> list[int]:
        with self._lock:
            self._ensure_open("append")
            if self._access != "write":
                raise SessionReadOnlyError(self._header.id, "append")
            if not events:
                return []
            rowids: list[int] = []
            for m in events:
                rowids.append(self._store._log.append(self._header.id, m))
            # Contiguidad: los rowids de un batch deben ser secuenciales.
            if rowids[-1] - rowids[0] + 1 != len(rowids) or any(
                b - a != 1 for a, b in zip(rowids, rowids[1:])
            ):
                raise SessionCorruptionError(
                    f"non-contiguous append for session {self._header.id!r}: rowids={rowids}"
                )
            return rowids

    def flush(self) -> None:
        with self._lock:
            self._ensure_open("flush")
            if self._access != "write":
                raise SessionReadOnlyError(self._header.id, "flush")
            self._store._flush_db()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._access == "write":
                    # close drena durabilidad igual que flush (upstream).
                    self._store._flush_db()
            finally:
                self._closed = True
                self._store._release(self._header.id, self)

    def __enter__(self) -> SessionHandle:
        return self

    def __exit__(self, *args) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class SessionStore:
    """SessionLog + versionado + handles single-writer.

    Usa SessionLog como backend read/append sin modificarlo.
    """

    def __init__(self, db_path: str | Path = "./data/session.db"):
        self._log = SessionLog(db_path)
        self._db_path = db_path
        self._lock = threading.RLock()
        self._write_owners: dict[str, SessionHandle] = {}
        self._last_repair: dict[str, RepairReport] = {}
        with self._lock:
            self._log._conn.executescript(_FORMAT_SCHEMA)

    # -- internas --------------------------------------------------------
    def _get_version(self, session_id: str) -> Optional[int]:
        row = self._log._conn.execute(
            "SELECT version FROM session_format WHERE session_id=?", (session_id,)
        ).fetchone()
        return row[0] if row else None

    def _check_version(self, session_id: str) -> int:
        version = self._get_version(session_id)
        if version is None:
            # Sesion creada por SessionLog crudo (sin fila de formato): se adopta
            # como v0 actual y se registra. Solo v0 existe, asi que adoptar es seguro.
            version = SESSION_FORMAT_VERSION
            with self._lock:
                self._log._conn.execute(
                    "INSERT OR IGNORE INTO session_format(session_id, version, created_at) VALUES (?,?,?)",
                    (session_id, version, datetime.now(timezone.utc).isoformat()),
                )
        if version != SESSION_FORMAT_VERSION:
            raise SessionFormatUnsupportedError(_version_refusal(session_id, version))
        return version

    def _repair(self, session_id: str) -> RepairReport:
        """Crash-recovery del lector: detecta turnos abiertos en memoria.

        NUNCA trunca ni borra: solo observa y reporta. El conteo de mensajes
        queda intacto.
        """
        before = self._log.count_messages(session_id)
        depth = 0
        for m in self._log.get_messages(session_id, limit=10_000_000):
            if m.role == "tool" and m.tool_name == TURN_START:
                depth += 1
            elif m.role == "tool" and m.tool_name == TURN_END:
                depth = max(0, depth - 1)
        after = self._log.count_messages(session_id)
        if after != before:  # defensa: repair no debe mutar jamas
            raise SessionCorruptionError(f"repair mutated session {session_id!r}")
        report = RepairReport(repaired=depth > 0, open_turns=depth)
        self._last_repair[session_id] = report
        return report

    def _flush_db(self) -> None:
        with self._lock:
            try:
                self._log._conn.commit()
            except Exception as e:
                raise SessionCorruptionError(f"flush commit failed: {e}") from e
            if str(self._db_path) == ":memory:":
                return
            try:
                self._log._conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception:
                pass
            try:
                # O_RDWR: en Windows fsync sobre fd O_RDONLY da EBADF.
                fd = os.open(str(self._db_path), os.O_RDWR)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except FileNotFoundError:
                pass
            except OSError as e:
                raise SessionCorruptionError(f"flush fsync failed: {e}") from e

    def _release(self, session_id: str, handle: SessionHandle) -> None:
        with self._lock:
            if self._write_owners.get(session_id) is handle:
                del self._write_owners[session_id]

    # -- API -------------------------------------------------------------
    def create(self, session_id: Optional[str] = None, project: str = "default") -> SessionHandle:
        sid = session_id or uuid.uuid4().hex[:12]
        with self._lock:
            if self._log.get_session(sid) is not None:
                raise SessionAlreadyExistsError(sid)
            if sid in self._write_owners:
                raise SessionAlreadyOwnedError(sid)
            sess = self._log.create_session(project=project, session_id=sid)
            header = SessionHeader(id=sess.id, version=SESSION_FORMAT_VERSION, project=sess.project)
            self._log._conn.execute(
                "INSERT INTO session_format(session_id, version, created_at) VALUES (?,?,?)",
                (header.id, header.version, header.created_at),
            )
            handle = SessionHandle(self, header, "write")
            self._write_owners[sid] = handle
            return handle

    def open(self, session_id: str, access: SessionAccess = "read") -> SessionHandle:
        with self._lock:
            sess = self._log.get_session(session_id)
            if sess is None:
                raise SessionNotFoundError(session_id)
            version = self._check_version(session_id)
            # Crash-recovery del lector en cada open (en memoria, sin truncar).
            self._repair(session_id)
            header = SessionHeader(id=sess.id, version=version, project=sess.project)
            if access == "write":
                owner = self._write_owners.get(session_id)
                if owner is not None and not owner.closed:
                    raise SessionAlreadyOwnedError(session_id)
                handle = SessionHandle(self, header, "write")
                self._write_owners[session_id] = handle
                return handle
            return SessionHandle(self, header, "read")

    def stat(self, session_id: str) -> SessionSnapshot:
        with self._lock:
            sess = self._log.get_session(session_id)
            if sess is None:
                raise SessionNotFoundError(session_id)
            version = self._check_version(session_id)
            row = self._log._conn.execute(
                "SELECT COUNT(*), MAX(created_at) FROM messages WHERE session_id=?", (session_id,)
            ).fetchone()
            count, max_ts = row[0] or 0, row[1] or ""
            revision = f"{count}:{max_ts}"
            return SessionSnapshot(
                header=SessionHeader(id=sess.id, version=version, project=sess.project),
                revision=revision,
                event_count=count,
            )

    def list(self, project: Optional[str] = None) -> list[SessionSnapshot]:
        sessions = self._log.list_sessions(project=project)
        out: list[SessionSnapshot] = []
        for s in sessions:
            try:
                out.append(self.stat(s.id))
            except SessionFormatUnsupportedError:
                raise
        return out

    def repair_report(self, session_id: str) -> Optional[RepairReport]:
        return self._last_repair.get(session_id)

    def close(self) -> None:
        with self._lock:
            for h in list(self._write_owners.values()):
                try:
                    h.close()
                except Exception:
                    pass
            self._write_owners.clear()
            self._log.close()

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *args) -> None:
        self.close()
