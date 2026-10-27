"""Auditoria de memoria: que entro en cada turno y si ese turno sirvio.

`memory_context` ya dejaba el evento `memory_used` con los node_ids inyectados,
pero el evento moria ahi: no habia forma de preguntar "esta memoria aparece
siempre que el turno falla". Aca se cierra el circulo en cuatro pasos:

  recall   un `batch` por turno con los nodos que viajaron al prompt
  outcome  el turno se cierra con su resultado real (ok / error)
  verdict  un nodo recibe veredictos: helpful / outdated / harmful / wrong
  standing el veredicto mueve un multiplicador que `memory_context` aplica al
           recuperar: la memoria que perjudica deja de viajar sin borrarse.

Nada de esto puede romper un turno: toda funcion publica traga sus errores y
devuelve un valor neutro. El archivo vive en el brain (o en
`NEXUS_V3_MEMORY_AUDIT`) porque es gobernanza de la memoria, no telemetria de UI.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Peso de cada veredicto. `wrong` pega mas que `harmful` porque una nota falsa
# que se repite ensena al modelo a equivocarse con seguridad.
VERDICT_WEIGHTS: dict[str, float] = {
    "helpful": 1.0,
    "outdated": -1.0,
    "harmful": -1.5,
    "wrong": -2.0,
}

_STANDING_FLOOR = 0.35
_STANDING_CEIL = 1.25
_STANDING_SLOPE = 0.5

_ready_paths: set[str] = set()
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# `timedelta(days=...)` revienta por encima de 999_999_999 dias y la API acepta
# cualquier int: `GET /api/memory/audit?days=100000000000` devolvia 500
# ("Python int too large to convert to C int") en vez de una ventana acotada.
# 100 anios es mas que cualquier retencion real.
MAX_REPORT_DAYS = 36500


def clamp_days(days: Any, default: int = 30) -> int:
    """Ventana en dias acotada a [1, MAX_REPORT_DAYS] (nunca lanza)."""
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = default
    return max(1, min(MAX_REPORT_DAYS, value))


def _since(days: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(days=clamp_days(days, 1))).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit_cfg() -> dict:
    """Config `memory.audit` (vacia si no se puede leer: nunca es fatal)."""
    try:
        from src.core.config import load_config

        cfg = load_config() or {}
    except Exception:
        return {}
    mem = cfg.get("memory") if isinstance(cfg, dict) else None
    audit = (mem or {}).get("audit") if isinstance(mem, dict) else None
    return audit if isinstance(audit, dict) else {}


def default_db_path() -> Path:
    env = (os.getenv("NEXUS_V3_MEMORY_AUDIT") or "").strip()
    if env:
        return Path(env)
    configured = str(audit_cfg().get("path") or "").strip()
    if configured:
        return Path(configured)
    try:
        from src.brain.link import brain_path

        return brain_path() / "memory_audit.db"
    except Exception:
        return Path("data") / "memory_audit.db"


class MemoryAudit:
    """Ledger de recuperacion + veredictos sobre una SQLite propia."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        # WAL + NORMAL: con FULL cada escritura paga fsync (~28 ms en D:\ias) y el cierre
        # de cada turno escribe varias filas (884 ms medidos en un turno de 8 s).
        con.execute("PRAGMA synchronous=NORMAL")
        key = str(self.db_path)
        if key not in _ready_paths:
            with _lock:
                if key not in _ready_paths:
                    self._init(con)
                    _ready_paths.add(key)
        return con

    def _init(self, con: sqlite3.Connection) -> None:
        con.execute(
            """CREATE TABLE IF NOT EXISTS recalls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                node_ids TEXT NOT NULL DEFAULT '[]',
                terms TEXT NOT NULL DEFAULT '[]',
                rerank TEXT NOT NULL DEFAULT '',
                injected_chars INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_recalls_batch ON recalls(batch)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_recalls_session ON recalls(session_id, created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_recalls_created ON recalls(created_at)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS outcomes (
                batch TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_status ON outcomes(status, created_at)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                batch TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT 'auto',
                created_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_verdicts_node ON verdicts(node_id)")
        con.commit()

    # ------------------------------------------------------------------ recall
    def record_recall(
        self,
        *,
        session_id: str = "",
        batch: str = "",
        node_ids: Optional[Iterable[Any]] = None,
        terms: Optional[Iterable[str]] = None,
        rerank: str = "",
        injected_chars: int = 0,
    ) -> int:
        """Una fila por turno con los nodos que viajaron. Devuelve cuantos guardo."""
        if not str(batch or "").strip():
            return 0
        ids: list[int] = []
        for nid in node_ids or []:
            try:
                value = int(nid)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in ids:
                ids.append(value)
        if not ids:
            return 0
        terms_list = [str(t)[:60] for t in (terms or []) if str(t).strip()][:16]
        try:
            con = self._connect()
            try:
                con.execute(
                    "INSERT INTO recalls (batch, session_id, node_ids, terms, rerank, injected_chars, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        str(batch)[:120],
                        str(session_id or "")[:120],
                        json.dumps(ids[:50]),
                        json.dumps(terms_list, ensure_ascii=False),
                        str(rerank or "")[:20],
                        max(0, int(injected_chars or 0)),
                        _now(),
                    ),
                )
                con.commit()
                return len(ids)
            finally:
                con.close()
        except Exception as e:
            logger.debug("memory_audit: recall no guardado (%s)", e)
            return 0

    def record_outcome(
        self, session_id: str = "", status: str = "ok", batch: str = "", note: str = ""
    ) -> bool:
        """Cierra el turno. Sin `batch` cierra el ultimo recall abierto de la sesion."""
        state = str(status or "ok").strip().lower()[:20]
        try:
            con = self._connect()
            try:
                target = str(batch or "").strip()
                if not target:
                    row = con.execute(
                        "SELECT batch FROM recalls WHERE session_id=? AND batch NOT IN"
                        " (SELECT batch FROM outcomes) ORDER BY id DESC LIMIT 1",
                        (str(session_id or ""),),
                    ).fetchone()
                    target = str(row["batch"]) if row else ""
                if not target:
                    return False
                con.execute(
                    "INSERT INTO outcomes (batch, session_id, status, note, created_at) VALUES (?,?,?,?,?)"
                    " ON CONFLICT(batch) DO UPDATE SET status=excluded.status,"
                    " note=excluded.note, created_at=excluded.created_at",
                    (target, str(session_id or "")[:120], state, str(note or "")[:500], _now()),
                )
                con.commit()
                return True
            finally:
                con.close()
        except Exception as e:
            logger.debug("memory_audit: outcome no guardado (%s)", e)
            return False

    # ---------------------------------------------------------------- verdicts
    def add_verdict(
        self,
        node_id: int,
        verdict: str,
        *,
        batch: str = "",
        note: str = "",
        origin: str = "auto",
    ) -> dict:
        name = str(verdict or "").strip().lower()
        if name not in VERDICT_WEIGHTS:
            raise ValueError(f"veredicto invalido: {verdict!r} (validos: {sorted(VERDICT_WEIGHTS)})")
        try:
            nid = int(node_id)
        except (TypeError, ValueError):
            raise ValueError("node_id (int) requerido")
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO verdicts (node_id, verdict, batch, note, origin, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (nid, name, str(batch or "")[:120], str(note or "")[:500],
                 str(origin or "auto")[:20], _now()),
            )
            con.commit()
        finally:
            con.close()
        return {
            "node_id": nid,
            "verdict": name,
            "weight": VERDICT_WEIGHTS[name],
            "standing": round(self.standing_map([nid]).get(nid, 1.0), 3),
        }

    # ---------------------------------------------------------------- standing
    def standing_map(self, node_ids: Optional[Iterable[Any]] = None) -> dict[int, float]:
        """Multiplicador por nodo: 1.0 sin veredictos, <1 si perjudica, >1 si ayuda."""
        if not audit_cfg().get("enabled", True):
            return {}
        ids: list[int] = []
        for nid in node_ids or []:
            try:
                value = int(nid)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in ids:
                ids.append(value)
        try:
            con = self._connect()
            try:
                if ids:
                    marks = ",".join("?" for _ in ids[:200])
                    rows = con.execute(
                        f"SELECT node_id, verdict, COUNT(*) n FROM verdicts"
                        f" WHERE node_id IN ({marks}) GROUP BY node_id, verdict",
                        tuple(ids[:200]),
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT node_id, verdict, COUNT(*) n FROM verdicts GROUP BY node_id, verdict"
                    ).fetchall()
            finally:
                con.close()
        except Exception as e:
            logger.debug("memory_audit: standing no calculado (%s)", e)
            return {}

        acc: dict[int, list[float]] = {}
        for row in rows:
            nid = int(row["node_id"])
            weight = VERDICT_WEIGHTS.get(str(row["verdict"]), 0.0)
            count = int(row["n"] or 0)
            bucket = acc.setdefault(nid, [0.0, 0.0])
            bucket[0] += weight * count
            bucket[1] += count

        out: dict[int, float] = {}
        for nid, (total, count) in acc.items():
            score = total / (1.0 + count)
            mult = 1.0 + _STANDING_SLOPE * score
            out[nid] = round(min(_STANDING_CEIL, max(_STANDING_FLOOR, mult)), 3)
        return out

    def min_standing(self) -> float:
        """Piso de standing: por debajo de esto el nodo no viaja al prompt.

        0.6 deja pasar un `harmful` (0.625: se hunde en el ranking pero sigue
        disponible) y corta un `wrong` (0.5: afirmacion falsa, fuera del turno).
        """
        try:
            value = float(audit_cfg().get("min_standing", 0.6))
        except (TypeError, ValueError):
            return 0.6
        return min(1.0, max(_STANDING_FLOOR, value))

    # ------------------------------------------------------------------ report
    def report(self, days: int = 30, limit: int = 20) -> dict:
        """Lo que la UI necesita: volumen, nodos que mas viajan y los castigados."""
        days = clamp_days(days)
        since = _since(days)
        limit = max(1, min(100, int(limit or 20)))
        con = self._connect()
        try:
            totals = {
                "recalls": int(con.execute(
                    "SELECT COUNT(*) c FROM recalls WHERE created_at>=?", (since,)
                ).fetchone()["c"] or 0),
                "turns_with_memory": int(con.execute(
                    "SELECT COUNT(DISTINCT batch) c FROM recalls WHERE created_at>=?", (since,)
                ).fetchone()["c"] or 0),
                "outcomes": int(con.execute(
                    "SELECT COUNT(*) c FROM outcomes WHERE created_at>=?", (since,)
                ).fetchone()["c"] or 0),
                "verdicts": int(con.execute("SELECT COUNT(*) c FROM verdicts").fetchone()["c"] or 0),
            }
            by_status = {
                str(r["status"]): int(r["n"])
                for r in con.execute(
                    "SELECT status, COUNT(*) n FROM outcomes WHERE created_at>=? GROUP BY status",
                    (since,),
                ).fetchall()
            }
            rows = con.execute(
                "SELECT batch, node_ids FROM recalls WHERE created_at>=?", (since,)
            ).fetchall()
            outcome_by_batch = {
                str(r["batch"]): str(r["status"])
                for r in con.execute(
                    "SELECT batch, status FROM outcomes WHERE created_at>=?", (since,)
                ).fetchall()
            }
            totals["with_outcome"] = len(outcome_by_batch)
        finally:
            con.close()

        used: dict[int, int] = {}
        turns: dict[int, int] = {}
        fails: dict[int, int] = {}
        for row in rows:
            try:
                ids = json.loads(row["node_ids"] or "[]")
            except (TypeError, ValueError):
                continue
            status = outcome_by_batch.get(str(row["batch"]))
            for nid in ids if isinstance(ids, list) else []:
                try:
                    nid = int(nid)
                except (TypeError, ValueError):
                    continue
                used[nid] = used.get(nid, 0) + 1
                if status:
                    turns[nid] = turns.get(nid, 0) + 1
                    if status != "ok":
                        fails[nid] = fails.get(nid, 0) + 1

        top_ids = sorted(used, key=lambda k: used[k], reverse=True)[:limit]
        standings = self.standing_map(top_ids)
        titles = _titles_for(top_ids)

        top = []
        for nid in top_ids:
            evaluated = turns.get(nid, 0)
            top.append({
                "node_id": nid,
                "title": titles.get(nid, ""),
                "recalls": used.get(nid, 0),
                "turns_with_outcome": evaluated,
                "fail_rate": round((fails.get(nid, 0) / evaluated), 3) if evaluated else None,
                "standing": standings.get(nid, 1.0),
            })

        penalized = [
            {"node_id": nid, "standing": mult, "title": titles.get(nid, "")}
            for nid, mult in sorted(self.standing_map().items(), key=lambda kv: kv[1])
            if mult < self.min_standing()
        ][:limit]

        return {
            "days": int(days),
            "totals": totals,
            "outcomes_by_status": by_status,
            "top_recalled": top,
            "penalized": penalized,
            "min_standing": self.min_standing(),
            "db": str(self.db_path),
        }

    # ------------------------------------------------------------------- prune
    def prune(self, days: int = 180) -> int:
        """Retencion del ledger. Los veredictos no se tocan: son gobernanza."""
        cutoff = _since(days)
        con = self._connect()
        try:
            removed = 0
            cur = con.execute("DELETE FROM recalls WHERE created_at < ? AND batch IN"
                              " (SELECT batch FROM outcomes WHERE created_at < ?)", (cutoff, cutoff))
            removed += int(cur.rowcount or 0)
            cur = con.execute("DELETE FROM outcomes WHERE created_at < ?", (cutoff,))
            removed += int(cur.rowcount or 0)
            con.commit()
            return removed
        finally:
            con.close()


def _titles_for(node_ids: list[int]) -> dict[int, str]:
    """Titulos del grafo para que el reporte se lea sin abrir otra pestaña."""
    if not node_ids:
        return {}
    try:
        from src.core.knowledge_memory import get_knowledge_memory

        km = get_knowledge_memory()
        con = km._connect()
        try:
            marks = ",".join("?" for _ in node_ids)
            rows = con.execute(
                f"SELECT id, title FROM nodes WHERE id IN ({marks})", tuple(node_ids)
            ).fetchall()
            return {int(r["id"]): str(r["title"] or "") for r in rows}
        finally:
            con.close()
    except Exception:
        return {}


_defaults: dict[str, MemoryAudit] = {}


def get_memory_audit(db_path: Optional[Path] = None) -> MemoryAudit:
    key = str(Path(db_path) if db_path else default_db_path())
    instance = _defaults.get(key)
    if instance is None:
        instance = MemoryAudit(Path(key))
        _defaults[key] = instance
    return instance


def record_recall(**kwargs: Any) -> int:
    try:
        return get_memory_audit().record_recall(**kwargs)
    except Exception as e:
        logger.debug("memory_audit: record_recall omitido (%s)", e)
        return 0


def record_outcome(*args: Any, **kwargs: Any) -> bool:
    try:
        return get_memory_audit().record_outcome(*args, **kwargs)
    except Exception as e:
        logger.debug("memory_audit: record_outcome omitido (%s)", e)
        return False


def standing_map(node_ids: Optional[Iterable[Any]] = None) -> dict[int, float]:
    try:
        return get_memory_audit().standing_map(node_ids)
    except Exception as e:
        logger.debug("memory_audit: standing omitido (%s)", e)
        return {}
