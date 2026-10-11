"""Session projections — folds puros + cache por revision (S1-paso3).

Porta la semantica upstream deepseek-harness sin tablas nuevas:
- ``fold_session_stats``: conteos por rol + tokens + wall time.
- ``fold_turn_outline``: turnos delimitados por marcadores
  ``turn:start`` / ``turn:end`` (role "tool").
- ``ProjectionCache``: cache en memoria con clave (session_id, revision),
  donde revision es el token opaco ``"COUNT:MAX(created_at)"`` de
  ``SessionStore.stat``.

Todo es PURO: las funciones reciben ``Sequence[Message]`` y nunca tocan
SQLite. La revision se pasa como parametro ya calculado por el llamante
(``SessionStore.stat(...).revision``). Sin DDL, sin tablas nuevas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

try:  # solo para tipado; nunca se usa en runtime (pureza)
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:  # pragma: no cover
        from src.core.session_log import Message
except Exception:  # pragma: no cover
    Message = object  # type: ignore

TURN_START = "turn:start"
TURN_END = "turn:end"


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SessionStats:
    total: int = 0
    counts: dict = field(default_factory=dict)  # rol -> conteo
    total_tokens: int = 0
    wall_seconds: float = 0.0
    first_at: str = ""
    last_at: str = ""


@dataclass(frozen=True, slots=True)
class TurnEntry:
    turn: int
    seq_start: int
    seq_end: int  # inclusivo; -1 si el turno quedo abierto al final
    message_count: int
    preview: str


@dataclass(frozen=True, slots=True)
class SessionOutline:
    session_id: str
    revision: str
    turns: tuple = ()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _preview(text: str, limit: int = 80) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


# ---------------------------------------------------------------------------
# Folds (puros y deterministas)
# ---------------------------------------------------------------------------
def fold_session_stats(messages: Sequence[Message]) -> SessionStats:
    """Fold determinista: conteos por rol + tokens + wall time.

    Wall time = (last.created_at - first.created_at) en segundos; 0.0 si
    hay < 2 mensajes o los timestamps no parsean.
    """
    counts: dict[str, int] = {}
    total_tokens = 0
    first_at = ""
    last_at = ""
    first_dt: datetime | None = None
    last_dt: datetime | None = None

    for m in messages:
        counts[m.role] = counts.get(m.role, 0) + 1
        total_tokens += int(getattr(m, "tokens", 0) or 0)
        ts = getattr(m, "created_at", "")
        dt = _parse_ts(ts)
        if dt is None:
            continue
        if first_dt is None:
            first_dt, first_at = dt, ts
        last_dt, last_at = dt, ts

    wall = 0.0
    if first_dt is not None and last_dt is not None:
        wall = max(0.0, (last_dt - first_dt).total_seconds())

    return SessionStats(
        total=len(messages),
        counts=dict(sorted(counts.items())),
        total_tokens=total_tokens,
        wall_seconds=wall,
        first_at=first_at,
        last_at=last_at,
    )


def fold_turn_outline(
    messages: Sequence[Message],
    preview_chars: int = 80,
) -> list[TurnEntry]:
    """Fold determinista de turnos.

    - ``turn:start`` (role tool) abre un turno nuevo (1-based).
    - ``turn:end`` cierra el turno abierto (si hay).
    - Los mensajes fuera de marcadores van al turno 0 (preambulo) o al
      turno abierto actual.
    - ``preview`` = primer mensaje ``user`` del turno, si hay; si no, el
      primer contenido no-marcador; si no, "".
    - ``seq_*`` son offsets 0-based sobre ``messages``.
    """
    buckets: dict[int, list[int]] = {}
    order: list[int] = []
    current = 0

    def _ensure(t: int) -> None:
        if t not in buckets:
            buckets[t] = []
            order.append(t)

    _ensure(0)
    for seq, m in enumerate(messages):
        is_marker = m.role == "tool" and m.tool_name in (TURN_START, TURN_END)
        if is_marker and m.tool_name == TURN_START:
            current += 1
            _ensure(current)
            buckets[current].append(seq)
        elif is_marker and m.tool_name == TURN_END:
            if current == 0:
                buckets[0].append(seq)  # end huerfano -> preambulo
            else:
                buckets[current].append(seq)
                current = 0
        else:
            _ensure(current)
            buckets[current].append(seq)

    entries: list[TurnEntry] = []
    for t in sorted(buckets):
        seqs = buckets[t]
        if not seqs:
            continue
        preview = ""
        for s in seqs:
            m = messages[s]
            if m.role == "tool" and m.tool_name in (TURN_START, TURN_END):
                continue
            if m.role == "user" and m.content.strip():
                preview = _preview(m.content, preview_chars)
                break
        if not preview:
            for s in seqs:
                m = messages[s]
                if m.role == "tool" and m.tool_name in (TURN_START, TURN_END):
                    continue
                if (m.content or "").strip():
                    preview = _preview(m.content, preview_chars)
                    break
        entries.append(
            TurnEntry(
                turn=t,
                seq_start=seqs[0],
                seq_end=seqs[-1],
                message_count=len(seqs),
                preview=preview,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Cache por revision
# ---------------------------------------------------------------------------
def make_cache_key(session_id: str, revision: str) -> tuple[str, str]:
    return (session_id, revision)


class ProjectionCache:
    """Cache en memoria clave=(session_id, revision).

    ``get_or_compute`` devuelve (stats, outline, hit). El llamante obtiene
    ``revision`` via ``SessionStore.stat(session_id).revision`` (live) y
    pasa los mensajes leidos con un handle read: si la revision cambio,
    es miss y se recomputa (reconciliacion live-preferente).
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], tuple[SessionStats, SessionOutline]] = {}

    def get_or_compute(
        self,
        session_id: str,
        revision: str,
        messages: Sequence[Message],
        preview_chars: int = 80,
    ) -> tuple[SessionStats, SessionOutline, bool]:
        key = make_cache_key(session_id, revision)
        hit = self._store.get(key)
        if hit is not None:
            return hit[0], hit[1], True
        stats = fold_session_stats(messages)
        turns = tuple(fold_turn_outline(messages, preview_chars))
        outline = SessionOutline(session_id=session_id, revision=revision, turns=turns)
        self._store[key] = (stats, outline)
        return stats, outline, False

    def needs_refresh(self, session_id: str, cached_revision: str, live_revision: str) -> bool:
        """True si el vivo manda: la proyeccion cacheada esta rancia."""
        _ = session_id
        return cached_revision != live_revision

    def invalidate(self, session_id: str) -> int:
        doomed = [k for k in self._store if k[0] == session_id]
        for k in doomed:
            del self._store[k]
        return len(doomed)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._store)
