"""Session query API — filtros / trazas / search con snippet + cursor (S1-paso3).

Semantica portada de upstream deepseek-harness:
- Filtros AND entre dimensiones (id/project/cwd/parent), OR dentro de
  listas (``ids=[...]``).
- Trazas: lineage (ancestros via parent + hijos) y event trace paginado.
- Search cross-session con snippet + paginacion por cursor opaco.
- Reconciliacion live-preferente: la revision viva de ``SessionStore.stat``
  manda sobre cualquier proyeccion cacheada.

RESTRICCION FTS: este modulo NUNCA altera ni reindexa tablas FTS
existentes (no ejecuta CREATE/DROP/ALTER sobre ``session_fts`` ni
``..._rebuild``). Solo hace SELECT sobre el FTS existente via la API
publica de ``SessionLog`` (``search``/``search_session``) y genera
snippets en Python. ``ensure_query_index`` solo crea una tabla auxiliar
regular (no-FTS) con ``CREATE TABLE IF NOT EXISTS`` para cursores
persistidos futuros; es aditiva y no toca el FTS.
"""

from __future__ import annotations

import base64
import sqlite3
from dataclasses import dataclass, field
from typing import Optional, Sequence

from src.core.session_log import Message, Session, SessionLog

# Columnas candidatas de cwd si algun schema futuro la agrega. Si no existe
# ninguna, el filtro cwd usa fallback LIKE sobre messages (sin DDL).
_CWD_CANDIDATES = ("cwd", "workdir", "working_dir", "workingdir", "directory")

_AUX_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_query_aux (
  session_id TEXT PRIMARY KEY,
  last_revision TEXT NOT NULL DEFAULT ''
);
"""


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SearchHit:
    session_id: str
    role: str
    snippet: str
    seq: int = -1  # offset en get_messages si se pudo resolver, -1 si no
    rank: float = 0.0


@dataclass(frozen=True, slots=True)
class SearchPage:
    hits: tuple = ()
    next_cursor: str = ""
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class Lineage:
    session: Session | None
    ancestors: tuple = ()  # raiz -> padre directo
    children: tuple = ()


# ---------------------------------------------------------------------------
# Cursores opacos (base64 de un offset int)
# ---------------------------------------------------------------------------
def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(int(offset)).encode()).decode()


def decode_cursor(cursor: str) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Snippets (Python puro, sin FTS highlight)
# ---------------------------------------------------------------------------
def make_snippet(content: str, query: str, context: int = 50) -> str:
    text = content or ""
    q = (query or "").strip()
    if not q:
        flat = " ".join(text.split())
        return flat[:100] + ("…" if len(flat) > 100 else "")
    # Primer termino que matchee (soporta "a OR b" / frases con comillas).
    terms = [t.strip(' "') for t in q.replace(" OR ", " ").split()]
    terms = [t for t in terms if t]
    low = text.lower()
    pos, term = -1, ""
    for t in terms:
        p = low.find(t.lower())
        if p != -1 and (pos == -1 or p < pos):
            pos, term = p, t
    if pos == -1:
        flat = " ".join(text.split())
        return flat[:100] + ("…" if len(flat) > 100 else "")
    start = max(0, pos - context)
    end = min(len(text), pos + len(term) + context)
    frag = text[start:end]
    if start > 0:
        frag = "…" + frag.lstrip()
    if end < len(text):
        frag = frag.rstrip() + "…"
    return " ".join(frag.split())


# ---------------------------------------------------------------------------
# FTS: garantia aditiva
# ---------------------------------------------------------------------------
def ensure_query_index(log: SessionLog) -> None:
    """Crea SOLO la tabla auxiliar regular (no-FTS). Nunca toca session_fts."""
    with log._lock:
        log._conn.executescript(_AUX_SCHEMA)


def fts_schema_fingerprint(log: SessionLog) -> str:
    """Huella del schema FTS existente (para tests: no debe cambiar)."""
    with log._lock:
        rows = log._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name IN "
            "('session_fts','messages_ai','messages_ad','messages_au') ORDER BY name"
        ).fetchall()
    return "|".join(r[0] or "" for r in rows)


# ---------------------------------------------------------------------------
# Filtros (AND entre dimensiones, OR dentro de ids)
# ---------------------------------------------------------------------------
def _table_columns(log: SessionLog, table: str) -> set[str]:
    with log._lock:
        rows = log._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def filter_sessions(
    log: SessionLog,
    ids: Optional[Sequence[str]] = None,
    id_prefix: Optional[str] = None,
    project: Optional[str] = None,
    cwd: Optional[str] = None,
    parent_id: Optional[str] = None,
    limit: int = 100,
) -> list[Session]:
    """Filtra sesiones: AND entre dimensiones, OR dentro de ``ids``."""
    sessions = log.list_sessions(project=project, limit=10_000)
    if ids is not None:
        wanted = set(ids)
        sessions = [s for s in sessions if s.id in wanted]
    if id_prefix:
        sessions = [s for s in sessions if s.id.startswith(id_prefix)]
    if parent_id is not None:
        sessions = [s for s in sessions if (s.parent_id or "") == parent_id]
    if cwd is not None:
        cols = _table_columns(log, "sessions")
        hit_col = next((c for c in _CWD_CANDIDATES if c in cols), None)
        if hit_col is not None:
            with log._lock:
                rows = log._conn.execute(
                    f"SELECT id FROM sessions WHERE {hit_col}=? LIMIT 10000", (cwd,)
                ).fetchall()
            keep = {r[0] for r in rows}
            sessions = [s for s in sessions if s.id in keep]
        else:
            # Fallback sin DDL: sesiones con algun mensaje que contenga cwd.
            keep: set[str] = set()
            with log._lock:
                try:
                    rows = log._conn.execute(
                        "SELECT DISTINCT session_id FROM messages WHERE content LIKE ? LIMIT 10000",
                        (f"%{cwd}%",),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            keep = {r[0] for r in rows}
            sessions = [s for s in sessions if s.id in keep]
    return sessions[: max(0, limit)]


# ---------------------------------------------------------------------------
# Trazas: lineage + event trace
# ---------------------------------------------------------------------------
def get_lineage(log: SessionLog, session_id: str, depth: int = 50) -> Lineage:
    """Ancestros (raiz->padre) via parent_session_id + hijos directos."""
    me = log.get_session(session_id)
    ancestors: list[Session] = []
    seen = {session_id}
    cur = me
    for _ in range(max(0, depth)):
        pid = (cur.parent_id or "") if cur else ""
        if not pid or pid in seen:
            break
        seen.add(pid)
        parent = log.get_session(pid)
        if parent is None:
            break
        ancestors.append(parent)
        cur = parent
    ancestors.reverse()
    children: list[Session] = []
    for s in log.list_sessions(limit=10_000):
        if (s.parent_id or "") == session_id:
            children.append(s)
    children.sort(key=lambda s: s.created_at)
    return Lineage(session=me, ancestors=tuple(ancestors), children=tuple(children))


def get_event_trace(
    log: SessionLog, session_id: str, offset: int = 0, limit: int = 50
) -> list[dict]:
    """Traza de eventos paginada con seq (offset 0-based)."""
    if offset < 0:
        raise ValueError(f"invalid offset {offset}")
    msgs = log.get_messages(session_id, limit=max(0, limit), offset=max(0, offset))
    out: list[dict] = []
    for i, m in enumerate(msgs):
        out.append({"seq": offset + i, "role": m.role, "tool_name": m.tool_name, "preview": make_snippet(m.content, "", 100), "created_at": m.created_at})
    return out


# ---------------------------------------------------------------------------
# Search con snippet + paginacion cursor (cross-session o por sesion)
# ---------------------------------------------------------------------------
def search_sessions(
    log: SessionLog,
    query: str,
    session_id: Optional[str] = None,
    limit: int = 20,
    cursor: str = "",
) -> SearchPage:
    """Busca via FTS existente (solo SELECT) y pagina en Python.

    ``cursor`` = offset opaco. Se pide ``offset+limit+1`` al backend para
    saber si hay mas (has_more/next_cursor). Snippets generados en Python.
    """
    if not query or not query.strip() or limit <= 0:
        return SearchPage(hits=(), next_cursor="", has_more=False)
    offset = decode_cursor(cursor)
    fetch = offset + limit + 1
    try:
        if session_id is not None:
            msgs = log.search_session(session_id, query, limit=fetch)
            sids = [session_id] * len(msgs)
        else:
            msgs, sids = _cross_session_search(log, query, fetch)
    except Exception:
        return SearchPage(hits=(), next_cursor="", has_more=False)

    window = msgs[offset : offset + limit + 1]
    has_more = len(window) > limit
    window = window[:limit]
    swindow = sids[offset : offset + limit] if len(sids) == len(msgs) else [session_id or ""] * len(window)
    hits: list[SearchHit] = []
    for m, sid in zip(window, swindow):
        seq = _resolve_seq(log, sid, m)
        hits.append(SearchHit(session_id=sid, role=m.role, snippet=make_snippet(m.content, query), seq=seq))
    next_cursor = encode_cursor(offset + limit) if has_more else ""
    return SearchPage(hits=tuple(hits), next_cursor=next_cursor, has_more=has_more)


def _cross_session_search(log: SessionLog, query: str, fetch: int) -> tuple[list[Message], list[str]]:
    """Cross-session usando solo SELECTs sobre messages/session_fts.

    Intenta FTS directo con session_id; fallback a LIKE. No muta nada.
    """
    msgs: list[Message] = []
    sids: list[str] = []
    with log._lock:
        try:
            rows = log._conn.execute(
                "SELECT m.session_id, m.role, m.content, m.tool_call_id, m.tool_name,"
                " m.tokens, m.model, m.created_at"
                " FROM session_fts JOIN messages m ON m.id=session_fts.rowid"
                " WHERE session_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, fetch),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = log._conn.execute(
                "SELECT session_id, role, content, tool_call_id, tool_name, tokens, model, created_at"
                " FROM messages WHERE content LIKE ? LIMIT ?",
                (f"%{query}%", fetch),
            ).fetchall()
    for r in rows:
        sids.append(r[0])
        msgs.append(Message(role=r[1], content=r[2], tool_call_id=r[3], tool_name=r[4], tokens=r[5], model=r[6], created_at=r[7]))
    return msgs, sids


def _resolve_seq(log: SessionLog, session_id: str, target: Message) -> int:
    """Resuelve el seq del mensaje (best-effort, -1 si ambiguo)."""
    if not session_id:
        return -1
    try:
        all_msgs = log.get_messages(session_id, limit=100_000)
    except Exception:
        return -1
    for i, m in enumerate(all_msgs):
        if m.content == target.content and m.role == target.role and m.tool_name == target.tool_name:
            return i
    return -1


# ---------------------------------------------------------------------------
# Reconciliacion live-preferente
# ---------------------------------------------------------------------------
def needs_refresh(cached_revision: str, live_revision: str) -> bool:
    """True si lo vivo manda: hay que recomputar la proyeccion."""
    return cached_revision != live_revision
