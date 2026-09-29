"""
F3: Checkpoint Recovery + Auto-Trigger

Save state at each node, resume from checkpoint on crash.
Auto-triggers checkpoints at 20%/45%/70% context pressure.
Supports budgeted context injection from checkpoints.
"""

import json
import logging
import sqlite3
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nexus-checkpoint")

# Auto-trigger thresholds matching MiMo Code pattern
AUTO_CHECKPOINT_THRESHOLDS = [0.20, 0.45, 0.70]


@dataclass
class Checkpoint:
    id: str
    run_id: str
    node_id: str
    state: Dict
    created_at: str = ""
    data_buffer: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


class CheckpointStore:
    """Persistent checkpoint storage with crash recovery + auto-trigger"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path.home() / ".nexus" / "brain" / "checkpoints.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            data_buffer TEXT DEFAULT ''
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS run_status (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            goal TEXT DEFAULT '',
            started_at TEXT NOT NULL,
            completed_at TEXT DEFAULT '',
            last_checkpoint_id TEXT DEFAULT ''
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id)")

        c.execute("""CREATE TABLE IF NOT EXISTS auto_checkpoint_log (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            threshold REAL NOT NULL,
            usage_pct REAL NOT NULL,
            session_id TEXT DEFAULT '',
            project TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_auto_cp_run ON auto_checkpoint_log(run_id)")

        c.execute("""CREATE TABLE IF NOT EXISTS checkpoint_context (
            run_id TEXT PRIMARY KEY,
            summary TEXT DEFAULT '',
            decisions TEXT DEFAULT '[]',
            files_touched TEXT DEFAULT '[]',
            pending_work TEXT DEFAULT '',
            token_count INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )""")
        conn.commit()
        conn.close()

    def save_checkpoint(self, run_id: str, node_id: str, state: Dict, data_buffer: str = "") -> Checkpoint:
        import uuid
        cp = Checkpoint(
            id=str(uuid.uuid4())[:12],
            run_id=run_id,
            node_id=node_id,
            state=state,
            data_buffer=data_buffer,
        )
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""INSERT OR REPLACE INTO checkpoints (id, run_id, node_id, state, created_at, data_buffer)
            VALUES (?, ?, ?, ?, ?, ?)""", (
            cp.id, cp.run_id, cp.node_id,
            json.dumps(cp.state, ensure_ascii=False),
            cp.created_at, cp.data_buffer,
        ))
        c.execute("""INSERT OR REPLACE INTO run_status (run_id, status, started_at, last_checkpoint_id)
            VALUES (?, 'running', ?, ?)""", (run_id, datetime.now().isoformat(), cp.id))
        conn.commit()
        conn.close()
        logger.debug(f"Checkpoint saved: {cp.id} (run: {run_id}, node: {node_id})")
        return cp

    def mark_run_complete(self, run_id: str):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE run_status SET status = 'completed', completed_at = ? WHERE run_id = ?",
                  (datetime.now().isoformat(), run_id))
        conn.commit()
        conn.close()

    def get_incomplete_runs(self) -> List[Dict]:
        """Find runs that didn't complete (potential crashes)"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM run_status WHERE status != 'completed' ORDER BY started_at DESC")
        runs = [dict(r) for r in c.fetchall()]
        conn.close()
        return runs

    def get_latest_checkpoint(self, run_id: str, node_id: str = None) -> Optional[Checkpoint]:
        """Get the latest checkpoint for a run (optionally for a specific node)"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if node_id:
            c.execute("SELECT * FROM checkpoints WHERE run_id = ? AND node_id = ? ORDER BY created_at DESC LIMIT 1",
                      (run_id, node_id))
        else:
            c.execute("SELECT * FROM checkpoints WHERE run_id = ? ORDER BY created_at DESC LIMIT 1", (run_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        return Checkpoint(
            id=row["id"], run_id=row["run_id"], node_id=row["node_id"],
            state=json.loads(row["state"]), created_at=row["created_at"],
            data_buffer=row["data_buffer"],
        )

    def get_all_checkpoints(self, run_id: str) -> List[Checkpoint]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM checkpoints WHERE run_id = ? ORDER BY created_at", (run_id,))
        rows = c.fetchall()
        conn.close()
        return [
            Checkpoint(id=r["id"], run_id=r["run_id"], node_id=r["node_id"],
                      state=json.loads(r["state"]), created_at=r["created_at"],
                      data_buffer=r["data_buffer"])
            for r in rows
        ]

    def cleanup_old_checkpoints(self, max_age_hours: int = 24):
        """Remove checkpoints older than max_age_hours"""
        cutoff = (datetime.now().timestamp() - max_age_hours * 3600)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("DELETE FROM checkpoints WHERE created_at < ?", (datetime.fromtimestamp(cutoff).isoformat(),))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        if deleted:
            logger.info(f"Cleaned up {deleted} old checkpoints")
        return deleted

    # ─── Auto-Trigger Checkpoint ────────────────────────────────────────

    def check_and_auto_checkpoint(self, session_manager, session_id: str = None,
                                    project: str = "default", node_id: str = "auto") -> Optional[Dict]:
        """
        Check context pressure and auto-save checkpoint at 20%/45%/70% thresholds.
        Returns checkpoint info if triggered, None otherwise.
        """
        pressure = session_manager.get_context_pressure(session_id)
        usage_pct = pressure["usage_percent"] / 100.0

        for threshold in sorted(AUTO_CHECKPOINT_THRESHOLDS):
            if usage_pct >= threshold:
                run_id = f"auto_{project}_{datetime.now().strftime('%Y%m%d')}"

                # Check if this threshold was already triggered for this run
                triggered = self._get_triggered_levels(run_id)
                if threshold not in triggered:
                    state = self._get_session_context(session_manager, session_id, project)
                    cp = self.save_checkpoint(run_id, node_id, state, data_buffer=f"auto@{threshold:.0%}")
                    self._log_auto_checkpoint(run_id, threshold, usage_pct,
                                              session_id or pressure["session_id"], project)
                    logger.info(f"Auto-checkpoint triggered at {threshold:.0%} context: {cp.id}")

                    # Update checkpoint context
                    self._update_checkpoint_context(run_id, state, pressure)

                    return {
                        "checkpoint_id": cp.id,
                        "threshold": threshold,
                        "usage_pct": usage_pct,
                        "run_id": run_id,
                        "session_id": session_id or pressure["session_id"],
                    }

        return None

    def _get_session_context(self, session_manager, session_id: str = None,
                              project: str = "default") -> Dict:
        """Extract relevant session state for checkpoint"""
        pressure = session_manager.get_context_pressure(session_id)
        session = session_manager.get_session(session_id)
        last_msgs = []
        for m in session.messages[-5:]:
            last_msgs.append({"role": m.role, "tokens": m.tokens, "model": m.model})
        return {
            "project": project,
            "session_id": session.id,
            "message_count": len(session.messages),
            "total_tokens": session.total_tokens,
            "max_tokens": session_manager.max_tokens,
            "compact_count": session.compact_count,
            "usage_pct": pressure["usage_percent"],
            "level": pressure["level"],
            "last_messages": last_msgs,
            "summary": session.summary,
            "timestamp": datetime.now().isoformat(),
        }

    def _get_triggered_levels(self, run_id: str) -> set:
        """Get already-triggered threshold levels for a run"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT DISTINCT threshold FROM auto_checkpoint_log WHERE run_id = ?", (run_id,))
        levels = {row[0] for row in c.fetchall()}
        conn.close()
        return levels

    def _log_auto_checkpoint(self, run_id: str, threshold: float,
                              usage_pct: float, session_id: str, project: str):
        import uuid
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""INSERT INTO auto_checkpoint_log (id, run_id, threshold, usage_pct, session_id, project, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""", (
            str(uuid.uuid4())[:12], run_id, threshold, usage_pct,
            session_id, project, datetime.now().isoformat(),
        ))
        conn.commit()
        conn.close()

    def _update_checkpoint_context(self, run_id: str, state: Dict, pressure: Dict):
        """Update persistent context summary for checkpoint"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""INSERT OR REPLACE INTO checkpoint_context
            (run_id, summary, token_count, updated_at)
            VALUES (?, ?, ?, ?)""", (
            run_id,
            f"Auto-checkpoint at {pressure['level']} pressure ({pressure['usage_percent']}%)",
            pressure["total_tokens"],
            datetime.now().isoformat(),
        ))
        conn.commit()
        conn.close()

    # ─── Budgeted Context Injection ──────────────────────────────────────

    def budgeted_inject(self, session_manager, session_id: str = None,
                         max_tokens: int = 2000, run_id: str = None) -> Optional[Dict]:
        """
        Find relevant checkpoint context and inject it as budget allows.
        Returns the injected context dict or None if no relevant checkpoint.
        """
        pressure = session_manager.get_context_pressure(session_id)
        if not run_id:
            run_id = f"auto_{session_manager.get_session(session_id).project}_{datetime.now().strftime('%Y%m%d')}"

        latest = self.get_latest_checkpoint(run_id)
        if not latest:
            return None

        state = latest.state
        context_parts = []
        budget = max_tokens * 4  # chars

        if state.get("summary"):
            summary = state["summary"]
            summary_chars = len(summary)
            if summary_chars <= budget:
                context_parts.append(f"[Context Summary]\n{summary}")
                budget -= summary_chars

        if budget > 200 and state.get("last_messages"):
            last_msg_str = json.dumps(state["last_messages"][-3:], ensure_ascii=False)
            if len(last_msg_str) <= budget:
                context_parts.append(f"[Recent Activity]\n{last_msg_str}")
                budget -= len(last_msg_str)

        if budget > 100:
            usage = f"[Context: {state.get('usage_pct', 0)}% used, {state.get('compact_count', 0)} compactions]"
            context_parts.append(usage)

        if not context_parts:
            return None

        return {
            "role": "system",
            "content": "\n\n".join(context_parts),
            "source": f"checkpoint:{latest.id}",
        }

    # ─── Context Reconstruction ─────────────────────────────────────────

    def reconstruct_context(self, session_manager, session_id: str = None,
                             run_id: str = None, max_tokens: int = 4000) -> Dict:
        """
        Reconstruct session context from checkpoints.
        Returns dict with summary, decisions, pending_work.
        """
        if not run_id:
            session = session_manager.get_session(session_id)
            run_id = f"auto_{session.project}_{datetime.now().strftime('%Y%m%d')}"

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("SELECT * FROM checkpoint_context WHERE run_id = ?", (run_id,))
        ctx = c.fetchone()
        conn.close()

        if ctx:
            return {
                "summary": ctx["summary"],
                "token_count": ctx["token_count"],
                "updated_at": ctx["updated_at"],
                "run_id": run_id,
            }

        return {"run_id": run_id, "summary": "", "note": "no checkpoint context found"}

    # ─── Auto-Checkpoint Status ─────────────────────────────────────────

    def get_auto_checkpoint_status(self, run_id: str = None) -> Dict:
        """Get status of auto-checkpoint triggers"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        if run_id:
            c.execute("""SELECT * FROM auto_checkpoint_log
                WHERE run_id = ? ORDER BY created_at""", (run_id,))
        else:
            c.execute("""SELECT * FROM auto_checkpoint_log
                ORDER BY created_at DESC LIMIT 20""")

        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return {
            "total_triggers": len(rows),
            "triggers": rows,
            "thresholds": AUTO_CHECKPOINT_THRESHOLDS,
        }

    def get_stats(self) -> Dict:
        base = self._base_stats()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        try:
            c.execute("SELECT COUNT(*) FROM auto_checkpoint_log")
            auto_triggers = c.fetchone()[0]
        except Exception:
            auto_triggers = 0
        conn.close()
        base["auto_triggers"] = auto_triggers
        base["thresholds"] = AUTO_CHECKPOINT_THRESHOLDS
        return base

    def _base_stats(self) -> Dict:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM checkpoints")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM run_status WHERE status = 'running'")
        running = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM run_status WHERE status = 'completed'")
        completed = c.fetchone()[0]
        conn.close()
        return {
            "total_checkpoints": total,
            "running_runs": running,
            "completed_runs": completed,
            "db_path": self.db_path,
        }

    def close(self):
        pass
