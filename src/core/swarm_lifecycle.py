"""
Swarm Lifecycle — token-aware context management for NexusHive.
Adapted from Hermes swarm-lifecycle.ts.

Gestiona el estado del contexto por tokens:
- healthy (0-250K) → normal
- watch (250K-400K) → preparar handoff
- handoff_required (400K-500K) → hacer handoff
- renew_required (>500K) → forzar renovación
"""

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ContextState(str, Enum):
    HEALTHY = "healthy"
    WATCH = "watch"
    HANDOFF_REQUIRED = "handoff_required"
    RENEW_REQUIRED = "renew_required"


@dataclass
class LifecyclePolicy:
    soft_tokens: int = 250_000
    handoff_tokens: int = 400_000
    hard_tokens: int = 500_000


DEFAULT_POLICY = LifecyclePolicy()

CONTEXT_THRESHOLDS = [
    (ContextState.HEALTHY, 0),
    (ContextState.WATCH, 250_000),
    (ContextState.HANDOFF_REQUIRED, 400_000),
    (ContextState.RENEW_REQUIRED, 500_000),
]


def get_context_state(total_tokens: int, policy: Optional[LifecyclePolicy] = None) -> ContextState:
    p = policy or DEFAULT_POLICY
    if total_tokens >= p.hard_tokens:
        return ContextState.RENEW_REQUIRED
    if total_tokens >= p.handoff_tokens:
        return ContextState.HANDOFF_REQUIRED
    if total_tokens >= p.soft_tokens:
        return ContextState.WATCH
    return ContextState.HEALTHY


def get_recommended_action(state: ContextState) -> str:
    return {
        ContextState.HEALTHY: "continue",
        ContextState.WATCH: "reduce_context",
        ContextState.HANDOFF_REQUIRED: "create_handoff",
        ContextState.RENEW_REQUIRED: "renew_session",
    }.get(state, "continue")


@dataclass
class LifecycleStatus:
    worker_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    context_state: ContextState = ContextState.HEALTHY
    recommended_action: str = "continue"
    policy: LifecyclePolicy = field(default_factory=lambda: DEFAULT_POLICY)
    handoff_exists: bool = False
    last_handoff_at: Optional[float] = None


class SwarmLifecycle:
    """Token-aware lifecycle manager for NexusHive workers."""

    def __init__(self, nexus_home: Optional[str] = None):
        home = nexus_home or os.environ.get("NEXUS_HOME", str(Path.home() / ".nexus"))
        self.root = Path(home) / "swarm"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "lifecycle.db"
        self._init_db()
        self._policies: Dict[str, LifecyclePolicy] = {}

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    worker_id TEXT PRIMARY KEY,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    started_at REAL,
                    last_handoff_at REAL,
                    handoff_count INTEGER DEFAULT 0
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def track_tokens(self, worker_id: str, input_tokens: int = 0, output_tokens: int = 0):
        total = input_tokens + output_tokens
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT * FROM sessions WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE sessions SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?,
                       total_tokens = total_tokens + ? WHERE worker_id = ?""",
                    (input_tokens, output_tokens, total, worker_id)
                )
            else:
                conn.execute(
                    "INSERT INTO sessions (worker_id, input_tokens, output_tokens, total_tokens, started_at) VALUES (?, ?, ?, ?, ?)",
                    (worker_id, input_tokens, output_tokens, total, time.time())
                )
            conn.commit()
        finally:
            conn.close()

    def get_status(self, worker_id: str) -> LifecycleStatus:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if not row:
                return LifecycleStatus(worker_id=worker_id)
            total = row["total_tokens"]
            policy = self._policies.get(worker_id, DEFAULT_POLICY)
            state = get_context_state(total, policy)
            return LifecycleStatus(
                worker_id=worker_id,
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                total_tokens=total,
                context_state=state,
                recommended_action=get_recommended_action(state),
                policy=policy,
                handoff_exists=row["handoff_count"] > 0,
                last_handoff_at=row["last_handoff_at"],
            )
        finally:
            conn.close()

    def set_policy(self, worker_id: str, policy: LifecyclePolicy):
        self._policies[worker_id] = policy

    def record_handoff(self, worker_id: str):
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE sessions SET last_handoff_at = ?, handoff_count = handoff_count + 1 WHERE worker_id = ?",
                (time.time(), worker_id)
            )
            conn.commit()
        finally:
            conn.close()

    def reset_session(self, worker_id: str):
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE sessions SET input_tokens = 0, output_tokens = 0, total_tokens = 0, started_at = ? WHERE worker_id = ?",
                (time.time(), worker_id)
            )
            conn.commit()
        finally:
            conn.close()

    def all_status(self) -> List[LifecycleStatus]:
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT * FROM sessions ORDER BY total_tokens DESC").fetchall()
            return [
                LifecycleStatus(
                    worker_id=r["worker_id"],
                    input_tokens=r["input_tokens"],
                    output_tokens=r["output_tokens"],
                    total_tokens=r["total_tokens"],
                    context_state=get_context_state(r["total_tokens"], self._policies.get(r["worker_id"])),
                    recommended_action=get_recommended_action(get_context_state(r["total_tokens"])),
                    handoff_exists=r["handoff_count"] > 0,
                    last_handoff_at=r["last_handoff_at"],
                )
                for r in rows
            ]
        finally:
            conn.close()
