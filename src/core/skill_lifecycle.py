"""
Skill Lifecycle Manager — Automatic state transitions for skills (inspired by Hermes)

Features:
  - ACTIVE -> STALE (30 days unused) -> ARCHIVED (90 days unused)
  - Pinned skills skip lifecycle transitions
  - Background loop for periodic scans
  - State change history tracking

Patrons:
  - Hermes skill_manage pin/unpin/curator workflow
  - auto_skill_creator.py (SkillState enum)
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus-skill-lifecycle")

# ─── Constants ────────────────────────────────────────────────────────

STALE_THRESHOLD_DAYS = 30
ARCHIVE_THRESHOLD_DAYS = 90

# ─── Enums ────────────────────────────────────────────────────────────


class LifecycleState(Enum):
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


class StateTransition(Enum):
    ACTIVE_TO_STALE = "active_to_stale"
    STALE_TO_ACTIVE = "stale_to_active"
    STALE_TO_ARCHIVED = "stale_to_archived"
    ARCHIVED_TO_ACTIVE = "archived_to_active"
    PINNED_SKIPPED = "pinned_skipped"


# ─── Dataclass ────────────────────────────────────────────────────────


@dataclass
class SkillLifecycleRecord:
    """Tracks lifecycle state for a skill."""

    skill_id: str
    state: LifecycleState = LifecycleState.ACTIVE
    pinned: bool = False
    last_used: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class StateChange:
    """Record of a lifecycle state transition."""

    skill_id: str
    from_state: LifecycleState
    to_state: LifecycleState
    reason: str
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = _now_iso()


# ─── Helpers ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    home = Path.home()
    db_dir = home / ".nexus" / "brain"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "skill_lifecycle.db"


def _days_since(iso_date: Optional[str]) -> Optional[float]:
    """Return days elapsed since an ISO date string, or None."""
    if not iso_date:
        return None
    try:
        dt = datetime.fromisoformat(iso_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


# ─── Singleton ────────────────────────────────────────────────────────
_singleton: Optional["SkillLifecycle"] = None
_singleton_lock = threading.Lock()


def get_lifecycle_manager() -> "SkillLifecycle":
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = SkillLifecycle()
    return _singleton


# ─── Core class ───────────────────────────────────────────────────────


class SkillLifecycle:
    """
    Skill Lifecycle Manager — transitions skills through ACTIVE -> STALE -> ARCHIVED.

    Usage:
        lm = get_lifecycle_manager()
        lm.pin(skill_id)
        changes = lm.scan_and_update()
        stats = lm.get_lifecycle_stats()
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or str(_db_path())
        self._local = threading.local()
        self._running = False
        self._init_db()

    # ── Connection management ─────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    # ── Schema ────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS skill_lifecycle (
                skill_id    TEXT PRIMARY KEY,
                state       TEXT NOT NULL DEFAULT 'active',
                pinned      INTEGER NOT NULL DEFAULT 0,
                last_used   TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS state_transitions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id    TEXT NOT NULL,
                from_state  TEXT NOT NULL,
                to_state    TEXT NOT NULL,
                reason      TEXT NOT NULL DEFAULT '',
                timestamp   TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sl_state ON skill_lifecycle(state);
            CREATE INDEX IF NOT EXISTS idx_sl_last_used ON skill_lifecycle(last_used);
            CREATE INDEX IF NOT EXISTS idx_st_skill ON state_transitions(skill_id);
            """
        )
        conn.commit()

    # ── Sync from external skill store ────────────────────────────────

    def _sync_skill(
        self,
        skill_id: str,
        state: str = "active",
        pinned: bool = False,
        last_used: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        """Upsert a skill into the lifecycle table (sync from auto_skill_creator or disk)."""
        now = _now_iso()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO skill_lifecycle
               (skill_id, state, pinned, last_used, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(skill_id) DO UPDATE SET
                   state = excluded.state,
                   pinned = excluded.pinned,
                   last_used = COALESCE(excluded.last_used, skill_lifecycle.last_used),
                   updated_at = excluded.updated_at""",
            (
                skill_id,
                state,
                int(pinned),
                last_used,
                created_at or now,
                now,
            ),
        )
        conn.commit()

    # ── Scan and update ───────────────────────────────────────────────

    def scan_and_update(self) -> List[StateChange]:
        """
        Scan all skills and transition states based on time thresholds.

        Returns list of state changes that were applied.
        Pinned skills are skipped.
        """
        conn = self._get_conn()
        now = _now_iso()
        changes: List[StateChange] = []

        rows = conn.execute("SELECT * FROM skill_lifecycle").fetchall()
        for row in rows:
            skill_id = row["skill_id"]
            current_state = LifecycleState(row["state"])
            pinned = bool(row["pinned"])
            last_used = row["last_used"]

            if pinned:
                continue

            days_idle = _days_since(last_used)
            # If never used, use created_at
            if days_idle is None:
                days_idle = _days_since(row["created_at"])

            if days_idle is None:
                continue

            new_state = current_state
            reason = ""

            if current_state == LifecycleState.ACTIVE:
                if days_idle >= STALE_THRESHOLD_DAYS:
                    new_state = LifecycleState.STALE
                    reason = f"Unused for {days_idle:.0f} days (threshold: {STALE_THRESHOLD_DAYS})"
            elif current_state == LifecycleState.STALE:
                if days_idle >= ARCHIVE_THRESHOLD_DAYS:
                    new_state = LifecycleState.ARCHIVED
                    reason = f"Idle for {days_idle:.0f} days (threshold: {ARCHIVE_THRESHOLD_DAYS})"

            if new_state != current_state:
                change = StateChange(
                    skill_id=skill_id,
                    from_state=current_state,
                    to_state=new_state,
                    reason=reason,
                )
                conn.execute(
                    """UPDATE skill_lifecycle
                       SET state = ?, updated_at = ?
                       WHERE skill_id = ?""",
                    (new_state.value, now, skill_id),
                )
                conn.execute(
                    """INSERT INTO state_transitions
                       (skill_id, from_state, to_state, reason, timestamp)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        skill_id,
                        current_state.value,
                        new_state.value,
                        reason,
                        change.timestamp,
                    ),
                )
                changes.append(change)
                logger.info(
                    "Skill %s: %s -> %s (%s)",
                    skill_id,
                    current_state.value,
                    new_state.value,
                    reason,
                )

        conn.commit()
        return changes

    # ── Pin / Unpin ───────────────────────────────────────────────────

    def pin(self, skill_id: str) -> bool:
        """Pin a skill to prevent lifecycle transitions."""
        conn = self._get_conn()
        cur = conn.execute(
            """UPDATE skill_lifecycle
               SET pinned = 1, updated_at = ?
               WHERE skill_id = ?""",
            (_now_iso(), skill_id),
        )
        conn.commit()
        if cur.rowcount > 0:
            logger.info("Pinned skill %s", skill_id)
        return cur.rowcount > 0

    def unpin(self, skill_id: str) -> bool:
        """Unpin a skill to allow lifecycle transitions."""
        conn = self._get_conn()
        cur = conn.execute(
            """UPDATE skill_lifecycle
               SET pinned = 0, updated_at = ?
               WHERE skill_id = ?""",
            (_now_iso(), skill_id),
        )
        conn.commit()
        if cur.rowcount > 0:
            logger.info("Unpinned skill %s", skill_id)
        return cur.rowcount > 0

    # ── Direct state manipulation ─────────────────────────────────────

    def mark_stale(self, skill_id: str) -> bool:
        """Manually transition a skill to STALE."""
        conn = self._get_conn()
        now = _now_iso()
        cur = conn.execute(
            """UPDATE skill_lifecycle
               SET state = ?, updated_at = ?
               WHERE skill_id = ? AND pinned = 0""",
            (LifecycleState.STALE.value, now, skill_id),
        )
        if cur.rowcount > 0:
            conn.execute(
                """INSERT INTO state_transitions
                   (skill_id, from_state, to_state, reason, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    skill_id,
                    LifecycleState.ACTIVE.value,
                    LifecycleState.STALE.value,
                    "Manually marked stale",
                    now,
                ),
            )
            conn.commit()
        return cur.rowcount > 0

    def mark_archived(self, skill_id: str) -> bool:
        """Manually transition a skill to ARCHIVED."""
        conn = self._get_conn()
        now = _now_iso()
        cur = conn.execute(
            """UPDATE skill_lifecycle
               SET state = ?, updated_at = ?
               WHERE skill_id = ? AND pinned = 0""",
            (LifecycleState.ARCHIVED.value, now, skill_id),
        )
        if cur.rowcount > 0:
            # Determine the from_state
            row = conn.execute(
                "SELECT state FROM skill_lifecycle WHERE skill_id = ?", (skill_id,)
            ).fetchone()
            from_state = LifecycleState(row["state"]) if row else LifecycleState.ACTIVE
            conn.execute(
                """INSERT INTO state_transitions
                   (skill_id, from_state, to_state, reason, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    skill_id,
                    from_state.value,
                    LifecycleState.ARCHIVED.value,
                    "Manually archived",
                    now,
                ),
            )
            conn.commit()
        return cur.rowcount > 0

    # ── Query ─────────────────────────────────────────────────────────

    def get_skill_state(self, skill_id: str) -> Optional[SkillLifecycleRecord]:
        """Get the current lifecycle record for a skill."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM skill_lifecycle WHERE skill_id = ?", (skill_id,)
        ).fetchone()
        if row is None:
            return None
        return SkillLifecycleRecord(
            skill_id=row["skill_id"],
            state=LifecycleState(row["state"]),
            pinned=bool(row["pinned"]),
            last_used=row["last_used"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── Stats ─────────────────────────────────────────────────────────

    def get_lifecycle_stats(self) -> Dict[str, Any]:
        """Aggregate statistics about skill lifecycle states."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM skill_lifecycle").fetchone()[0]
        by_state = {}
        for row in conn.execute(
            "SELECT state, COUNT(*) as cnt FROM skill_lifecycle GROUP BY state"
        ):
            by_state[row["state"]] = row["cnt"]
        pinned = conn.execute(
            "SELECT COUNT(*) FROM skill_lifecycle WHERE pinned = 1"
        ).fetchone()[0]
        total_transitions = conn.execute(
            "SELECT COUNT(*) FROM state_transitions"
        ).fetchone()[0]

        # Recent transitions
        recent = []
        for row in conn.execute(
            """SELECT * FROM state_transitions
               ORDER BY timestamp DESC LIMIT 10"""
        ):
            recent.append(
                {
                    "skill_id": row["skill_id"],
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "reason": row["reason"],
                    "timestamp": row["timestamp"],
                }
            )

        return {
            "total_skills": total,
            "by_state": by_state,
            "pinned": pinned,
            "total_transitions": total_transitions,
            "recent_transitions": recent,
        }

    # ── Background loop ───────────────────────────────────────────────

    def run_lifecycle_check(self, interval_hours: float = 24) -> None:
        """
        Blocking background loop that periodically scans and updates lifecycle states.

        Runs until stop_lifecycle_loop() is called.
        """
        self._running = True
        interval_seconds = interval_hours * 3600
        logger.info(
            "Starting lifecycle check loop (interval: %.1f hours)", interval_hours
        )

        while self._running:
            try:
                changes = self.scan_and_update()
                if changes:
                    logger.info(
                        "Lifecycle scan: %d state changes applied", len(changes)
                    )
                else:
                    logger.debug("Lifecycle scan: no changes needed")
            except Exception as exc:
                logger.error("Lifecycle scan failed: %s", exc, exc_info=True)

            # Sleep in short intervals so we can respond to stop quickly
            elapsed = 0.0
            while self._running and elapsed < interval_seconds:
                time.sleep(min(30, interval_seconds - elapsed))
                elapsed += 30

        logger.info("Lifecycle check loop stopped")

    def stop_lifecycle_loop(self) -> None:
        """Signal the background loop to stop."""
        self._running = False
