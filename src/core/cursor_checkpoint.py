"""
Cursor Checkpoint - Persistencia de estados agenciales para SuperNEXUS v2

Mecanismo de checkpoint/resume para congelar y reanudar estados agenciales
en caso de crash del sistema. Persiste:
- Iteracion actual del agente
- Acumulador de resultados (outputs)
- Ventana de deteccion de stall (recent_responses)
- Fingerprints de herramientas para deteccion de doom loops
- Input pendiente bloqueado

"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nexus-cursor")


@dataclass
class CursorState:
    """Estado del cursor para un agente"""
    agent_id: str
    iteration: int = 0
    task: str = ""
    outputs: Dict[str, Any] = field(default_factory=dict)
    recent_responses: List[str] = field(default_factory=list)
    recent_tool_fingerprints: List[List[List[str]]] = field(default_factory=list)
    pending_input: Optional[Dict[str, Any]] = None
    created_at: str = ""
    updated_at: str = ""
    status: str = "running"  # running, paused, completed, failed

    def __post_init__(self):
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now


class CursorStore:
    """
    Almacen de cursores con persistencia SQLite.

    Uso:
        store = CursorStore()
        store.save_cursor("code_agent", CursorState(iteration=5, outputs={"code": "..."}))
        state = store.load_cursor("code_agent")
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path.home() / ".nexus" / "brain" / "cursor.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS cursors (
            agent_id TEXT PRIMARY KEY,
            iteration INTEGER DEFAULT 0,
            task TEXT DEFAULT '',
            outputs TEXT DEFAULT '{}',
            recent_responses TEXT DEFAULT '[]',
            recent_tool_fingerprints TEXT DEFAULT '[]',
            pending_input TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT DEFAULT 'running'
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS cursor_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            iteration INTEGER,
            snapshot TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )""")
        conn.commit()
        conn.close()

    def save_cursor(self, agent_id: str, state: CursorState, snapshot: bool = False):
        """Guarda el cursor actual"""
        state.updated_at = datetime.now().isoformat()
        state.agent_id = agent_id

        conn = self._get_conn()
        c = conn.cursor()
        try:
            c.execute("""INSERT OR REPLACE INTO cursors 
                (agent_id, iteration, task, outputs, recent_responses, 
                 recent_tool_fingerprints, pending_input, created_at, updated_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                state.agent_id,
                state.iteration,
                state.task,
                json.dumps(state.outputs, default=str),
                json.dumps(state.recent_responses),
                json.dumps(state.recent_tool_fingerprints),
                json.dumps(state.pending_input, default=str) if state.pending_input else None,
                state.created_at,
                state.updated_at,
                state.status,
            ))

            # Guardar snapshot historico si se solicita
            if snapshot:
                c.execute("""INSERT INTO cursor_history (agent_id, iteration, snapshot, timestamp)
                    VALUES (?, ?, ?, ?)""", (
                    agent_id,
                    state.iteration,
                    json.dumps({
                        "iteration": state.iteration,
                        "task": state.task,
                        "outputs": state.outputs,
                        "status": state.status,
                    }, default=str),
                    datetime.now().isoformat(),
                ))

            conn.commit()
            logger.debug(f"Cursor saved: {agent_id} (iteration {state.iteration})")

        except Exception as e:
            logger.error(f"Error saving cursor for {agent_id}: {e}")
        finally:
            conn.close()

    def load_cursor(self, agent_id: str) -> Optional[CursorState]:
        """Carga el cursor de un agente"""
        conn = self._get_conn()
        c = conn.cursor()
        try:
            c.execute("SELECT * FROM cursors WHERE agent_id = ?", (agent_id,))
            row = c.fetchone()
            if not row:
                return None

            return CursorState(
                agent_id=row["agent_id"],
                iteration=row["iteration"],
                task=row["task"],
                outputs=json.loads(row["outputs"]) if row["outputs"] else {},
                recent_responses=json.loads(row["recent_responses"]) if row["recent_responses"] else [],
                recent_tool_fingerprints=json.loads(row["recent_tool_fingerprints"]) if row["recent_tool_fingerprints"] else [],
                pending_input=json.loads(row["pending_input"]) if row["pending_input"] else None,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                status=row["status"],
            )
        except Exception as e:
            logger.error(f"Error loading cursor for {agent_id}: {e}")
            return None
        finally:
            conn.close()

    def delete_cursor(self, agent_id: str) -> bool:
        """Elimina el cursor de un agente"""
        conn = self._get_conn()
        c = conn.cursor()
        try:
            c.execute("DELETE FROM cursors WHERE agent_id = ?", (agent_id,))
            conn.commit()
            return c.rowcount > 0
        finally:
            conn.close()

    def list_cursors(self, status: str = None) -> List[Dict]:
        """Lista todos los cursores"""
        conn = self._get_conn()
        c = conn.cursor()
        try:
            if status:
                c.execute("SELECT agent_id, iteration, task, status, updated_at FROM cursors WHERE status = ?", (status,))
            else:
                c.execute("SELECT agent_id, iteration, task, status, updated_at FROM cursors")
            return [dict(r) for r in c.fetchall()]
        finally:
            conn.close()

    def get_history(self, agent_id: str, limit: int = 10) -> List[Dict]:
        """Obtiene historial de snapshots de un agente"""
        conn = self._get_conn()
        c = conn.cursor()
        try:
            c.execute(
                "SELECT iteration, snapshot, timestamp FROM cursor_history WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
                (agent_id, limit),
            )
            return [dict(r) for r in c.fetchall()]
        finally:
            conn.close()

    def clear_history(self, older_than_days: int = 7):
        """Limpia historial antiguo"""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
        conn = self._get_conn()
        c = conn.cursor()
        try:
            c.execute("DELETE FROM cursor_history WHERE timestamp < ?", (cutoff,))
            conn.commit()
            deleted = c.rowcount
            logger.info(f"Cursor history cleaned: {deleted} entries removed")
            return deleted
        finally:
            conn.close()


class CursorCheckpoint:
    """
    Gestor de checkpoints para agentes.

    Integracion con LoopDetector y JudgePipeline para persistir
    estados de deteccion de loops y evaluaciones.

    Uso:
        checkpoint = CursorCheckpoint()
        checkpoint.save_state("code_agent", iteration=5, outputs={"code": "..."})
        state = checkpoint.restore_state("code_agent")
    """

    def __init__(self, store: CursorStore = None):
        self.store = store or CursorStore()

    def save_state(
        self,
        agent_id: str,
        iteration: int,
        task: str = "",
        outputs: Dict[str, Any] = None,
        recent_responses: List[str] = None,
        recent_tool_fingerprints: List[List[Tuple[str, str]]] = None,
        pending_input: Dict[str, Any] = None,
        status: str = "running",
        snapshot: bool = False,
    ):
        """Guarda estado completo del agente"""
        # Convertir fingerprints de tuple a list para JSON
        fps_json = []
        if recent_tool_fingerprints:
            for fp_list in recent_tool_fingerprints:
                fps_json.append([list(pair) for pair in fp_list])

        state = CursorState(
            agent_id=agent_id,
            iteration=iteration,
            task=task,
            outputs=outputs or {},
            recent_responses=recent_responses or [],
            recent_tool_fingerprints=fps_json,
            pending_input=pending_input,
            status=status,
        )
        self.store.save_cursor(agent_id, state, snapshot=snapshot)

    def restore_state(self, agent_id: str) -> Optional[Dict]:
        """Restaura estado del agente para resume"""
        state = self.store.load_cursor(agent_id)
        if not state:
            return None

        # Convertir fingerprints de list a tuple
        fps_tuples = []
        for fp_list in state.recent_tool_fingerprints:
            fps_tuples.append([tuple(pair) for pair in fp_list])

        logger.info(
            f"Restored agent {agent_id}: iteration={state.iteration}, "
            f"status={state.status}, stall_window={len(state.recent_responses)}, "
            f"doom_window={len(fps_tuples)}"
        )

        return {
            "iteration": state.iteration,
            "task": state.task,
            "outputs": state.outputs,
            "recent_responses": state.recent_responses,
            "recent_tool_fingerprints": fps_tuples,
            "pending_input": state.pending_input,
            "status": state.status,
        }

    def pause_agent(self, agent_id: str):
        """Pausa un agente"""
        state = self.store.load_cursor(agent_id)
        if state:
            state.status = "paused"
            self.store.save_cursor(agent_id, state)
            logger.info(f"Agent paused: {agent_id}")

    def resume_agent(self, agent_id: str) -> Optional[Dict]:
        """Reanuda un agente pausado"""
        state = self.store.load_cursor(agent_id)
        if state and state.status == "paused":
            state.status = "running"
            self.store.save_cursor(agent_id, state)
            logger.info(f"Agent resumed: {agent_id}")
            return self.restore_state(agent_id)
        return None

    def complete_agent(self, agent_id: str):
        """Marca un agente como completado"""
        state = self.store.load_cursor(agent_id)
        if state:
            state.status = "completed"
            self.store.save_cursor(agent_id, state, snapshot=True)
            logger.info(f"Agent completed: {agent_id}")

    def fail_agent(self, agent_id: str, error: str = ""):
        """Marca un agente como fallido"""
        state = self.store.load_cursor(agent_id)
        if state:
            state.status = "failed"
            if error:
                state.outputs["error"] = error
            self.store.save_cursor(agent_id, state, snapshot=True)
            logger.info(f"Agent failed: {agent_id}")

    def get_status(self) -> Dict:
        """Estado del sistema de checkpoints"""
        cursors = self.store.list_cursors()
        return {
            "total_cursors": len(cursors),
            "running": sum(1 for c in cursors if c.get("status") == "running"),
            "paused": sum(1 for c in cursors if c.get("status") == "paused"),
            "completed": sum(1 for c in cursors if c.get("status") == "completed"),
            "failed": sum(1 for c in cursors if c.get("status") == "failed"),
        }
