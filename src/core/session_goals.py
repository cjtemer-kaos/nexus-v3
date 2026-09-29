"""
session_goals.py — Patrón "Session Goals" adaptado de OpenChamber a latamrust-nexus.

Un objetivo (goal) le da a una tarea/sesión una "meta final" con presupuesto y un
AUDITOR que evalúa el resultado tras cada turno: el agente sigue trabajando hasta
completar, quedar bloqueado, o alcanzar el límite — incluso si se cierra la sesión.

Estado: active → paused → complete | blocked.
Cada goal: objective, tokenBudget, turnsUsed, blockedStreak, status, statusReason,
lastAccountedMessageID, createdAt, updatedAt.

Persistencia: SQLite (brain/cerebro.db, tabla session_goals). La meta/objetivo puede
vivir en un archivo aparte (peso ligero en la DB) si es larga.

Uso (formato de datos dict):
  { "id", "objective", "tokenBudget"|null, "status", "statusReason",
    "tokensUsed", "turnsUsed", "blockedStreak", "note",
    "lastAccountedMessageID", "createdAt", "updatedAt" }
"""
from __future__ import annotations

import json
import time
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

# Límite de caracteres del objetivo para el auditor (el agente vio el texto completo).
OBJECTIVE_CHAR_LIMIT = 4000


class SessionGoals:
    """Almacén y lógica de metas de sesión (patrón OpenChamber)."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path(__file__).resolve().parent.parent.parent / "brain" / "cerebro.db")
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS session_goals (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                objective TEXT DEFAULT '',
                objective_file INTEGER DEFAULT 0,
                token_budget INTEGER,
                tokens_used INTEGER DEFAULT 0,
                turns_used INTEGER DEFAULT 0,
                blocked_streak INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                status_reason TEXT DEFAULT '',
                note TEXT DEFAULT '',
                last_accounted_message_id TEXT DEFAULT '',
                created_at INTEGER,
                updated_at INTEGER
            )
            """
        )
        con.commit()
        con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def new_goal_id() -> str:
        return f"{int(time.time() * 1000):x}{os_random_hex()}"

    def set_goal(
        self,
        session_id: str,
        objective: str,
        token_budget: Optional[int] = None,
        existing: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Crea una meta nueva o edita la existente (mantiene contadores si se edita)."""
        objective = objective.strip()
        if not objective:
            raise ValueError("Goal objective must not be empty")
        objective_saved, objective_file = self._fit_objective(objective)
        now = int(time.time() * 1000)
        con = self._connect()
        if existing and existing.get("status") != "complete":
            # Editar en sitio: reactivar, limpiar estado de auditoría, resetear streak.
            con.execute(
                """UPDATE session_goals SET objective=?, objective_file=?, token_budget=?,
                   status='active', status_reason='resumed', blocked_streak=0, updated_at=?
                   WHERE session_id=? AND id=? """,
                (objective_saved, int(objective_file), token_budget, now, session_id, existing["id"]),
            )
            row = con.execute(
                "SELECT * FROM session_goals WHERE session_id=? AND id=?",
                (session_id, existing["id"]),
            ).fetchone()
        else:
            gid = self.new_goal_id()
            con.execute(
                """INSERT INTO session_goals
                   (id, session_id, objective, objective_file, token_budget, status,
                    status_reason, created_at, updated_at)
                   VALUES (?,?,?,?,?,'active','',?,?)""",
                (gid, session_id, objective_saved, int(objective_file), token_budget, now, now),
            )
            row = con.execute(
                "SELECT * FROM session_goals WHERE session_id=? AND id=?", (session_id, gid)
            ).fetchone()
        con.commit()
        goal = self._row_to_dict(row)
        con.close()
        return goal

    def set_status(
        self, session_id: str, goal_id: str,
        status: str, reason: str = "",
    ) -> Optional[Dict[str, Any]]:
        """active / paused / complete / blocked. Resume otorga contadores frescos."""
        con = self._connect()
        row = con.execute(
            "SELECT * FROM session_goals WHERE session_id=? AND id=?", (session_id, goal_id)
        ).fetchone()
        if not row:
            con.close()
            return None
        now = int(time.time() * 1000)
        # Un resume explícito otorga una nueva cuota de auto-continuación:
        # si no, una meta bloqueada en el tope de turnos se rebloquearía al siguiente tick.
        if status == "active":
            con.execute(
                """UPDATE session_goals SET status=?, status_reason=?, blocked_streak=0,
                   turns_used=0, updated_at=? WHERE session_id=? AND id=?""",
                (status, reason or "resumed", now, session_id, goal_id),
            )
        else:
            con.execute(
                """UPDATE session_goals SET status=?, status_reason=?, updated_at=?
                   WHERE session_id=? AND id=?""",
                (status, reason, now, session_id, goal_id),
            )
        con.commit()
        row = con.execute(
            "SELECT * FROM session_goals WHERE session_id=? AND id=?", (session_id, goal_id)
        ).fetchone()
        out = self._row_to_dict(row)
        con.close()
        return out

    def clear_goal(self, session_id: str, goal_id: str) -> bool:
        con = self._connect()
        was_active = con.execute(
            "SELECT status FROM session_goals WHERE session_id=? AND id=?",
            (session_id, goal_id),
        ).fetchone()
        active = bool(was_active and was_active["status"] == "active")
        con.execute("DELETE FROM session_goals WHERE session_id=? AND id=?", (session_id, goal_id))
        con.commit()
        con.close()
        return active  # True si había una meta activa (pausa = stop)

    def get_goal(self, session_id: str) -> Optional[Dict[str, Any]]:
        con = self._connect()
        row = con.execute(
            "SELECT * FROM session_goals WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        con.close()
        return self._row_to_dict(row) if row else None

    def account_turn(
        self, session_id: str, goal_id: str,
        tokens: int = 0, message_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Registra un turno/tokens usados tras un paso del agente."""
        con = self._connect()
        row = con.execute(
            "SELECT * FROM session_goals WHERE session_id=? AND id=?", (session_id, goal_id)
        ).fetchone()
        if not row:
            con.close()
            return None
        if row["last_accounted_message_id"] == message_id:
            con.close()
            return self._row_to_dict(row)  # mismo mensaje: no doble-contar
        now = int(time.time() * 1000)
        con.execute(
            """UPDATE session_goals SET turns_used=turns_used+1, tokens_used=tokens_used+?,
               last_accounted_message_id=?, updated_at=? WHERE session_id=? AND id=?""",
            (tokens, message_id, now, session_id, goal_id),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM session_goals WHERE session_id=? AND id=?", (session_id, goal_id)
        ).fetchone()
        out = self._row_to_dict(row)
        con.close()
        return out

    def audit(self, session_id: str, result_text: str, auditor_callable=None) -> Dict[str, Any]:
        """Auditor: evalúa la meta actual según el resultado del turno.

        auditor_callable(result_text, goal, objective_full) -> (complete:bool, reason:str)
        Si no se provee, usa heurística (presupuesto agotado → blocked/complete-limit).
        """
        goal = self.get_goal(session_id)
        if not goal:
            return {"audited": False, "reason": "no active goal"}
        if goal.get("status") == "complete":
            return {"audited": True, "decision": "complete", "reason": goal.get("status_reason")}
        if goal.get("status") == "blocked":
            return {"audited": True, "decision": "blocked", "reason": goal.get("status_reason")}

        # 1) Presupuesto agotado
        budget = goal.get("token_budget")
        if budget and (goal.get("tokens_used") or 0) >= budget:
            self.set_status(session_id, goal["id"], "blocked", "Presupuesto de tokens agotado")
            return {"audited": True, "decision": "blocked", "reason": "budget exhausted"}

        # 2) Turnos excesivos sin progreso → marca bloqueado (streak)
        streak = goal.get("blocked_streak") or 0
        if streak >= 3:
            self.set_status(session_id, goal["id"], "blocked", "Sin progreso tras 3 turnos")
            return {"audited": True, "decision": "blocked", "reason": "stalled"}

        # 3) Auditor externo (LLM) si se provee
        if auditor_callable:
            complete, reason = auditor_callable(
                result_text, goal, self._load_objective_full(goal)
            )
            if complete:
                self.set_status(session_id, goal["id"], "complete", reason or "auditor approved")
                return {"audited": True, "decision": "complete", "reason": reason}
            return {"audited": True, "decision": "continue", "reason": reason}

        return {"audited": True, "decision": "continue", "reason": "no external auditor"}

    # ------------------------------------------------------------------
    def _fit_objective(self, raw: str):
        """Objetivo ligero en la DB; si es largo, guarda en archivo aparte."""
        if len(raw) <= OBJECTIVE_CHAR_LIMIT:
            return raw, False
        # Archivo aparte por sesión: brain/goals/<session>.txt
        gdir = Path(self.db_path).parent / "goals"
        gdir.mkdir(parents=True, exist_ok=True)
        f = gdir / f"{abs(hash(raw))}.txt"
        f.write_text(raw, encoding="utf-8")
        return "", True

    def _load_objective_full(self, goal: Dict[str, Any]) -> str:
        if goal.get("objective"):
            return goal["objective"]
        # objective_file → buscar el archivo (heurística por hash es la única info)
        return goal.get("objective") or "(objective en archivo)"

    @staticmethod
    def _row_to_dict(row) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "objective": row["objective"],
            "objective_file": bool(row["objective_file"]),
            "token_budget": row["token_budget"],
            "tokens_used": row["tokens_used"],
            "turns_used": row["turns_used"],
            "blocked_streak": row["blocked_streak"],
            "status": row["status"],
            "status_reason": row["status_reason"],
            "note": row["note"],
            "last_accounted_message_id": row["last_accounted_message_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def os_random_hex() -> str:
    import os
    return os.urandom(3).hex()
