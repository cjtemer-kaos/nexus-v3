"""
Auto Skill Creator — Automatic skill creation from complex tasks (inspired by Hermes)

Features:
  - SQLite-backed skill storage with FTS5 full-text search
  - Auto-creation hook: analyzes completed tasks for reusable patterns
  - Skill lifecycle: ACTIVE/STALE/ARCHIVED states
  - Usage tracking: use_count, last_used timestamps
  - Pin/archive/delete operations

Patrons:
  - Hermes skill_manage/skill_view pattern
  - skill_extractor.py (existing auto-extraction)
  - fts5_search.py (FTS5 indexing pattern)
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus-auto-skill-creator")

# ─── Enums ────────────────────────────────────────────────────────────


class SkillState(Enum):
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


class CreatedBy(Enum):
    AGENT = "agent"
    USER = "user"
    CURATOR = "curator"


# ─── Dataclass ────────────────────────────────────────────────────────


@dataclass
class Skill:
    id: str
    name: str
    content: str  # full SKILL.md content
    state: SkillState = SkillState.ACTIVE
    pinned: bool = False
    use_count: int = 0
    last_used: Optional[str] = None
    created_by: CreatedBy = CreatedBy.AGENT
    category: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        now = _now_iso()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["created_by"] = self.created_by.value
        d["tags"] = json.dumps(self.tags)
        return d


# ─── Helpers ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    home = Path.home()
    db_dir = home / ".nexus" / "brain"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "skills_creator.db"


def _tag_extract(tool_calls: List[Dict[str, Any]]) -> List[str]:
    """Extract reusable tags from a list of tool call records."""
    tags = set()
    for tc in tool_calls:
        name = tc.get("name", tc.get("tool", ""))
        if name:
            tags.add(name.lower())
    return sorted(tags)


def _skill_name_from_task(task_result: str, context: str) -> str:
    """Derive a concise skill name from task result and context."""
    # Take first meaningful line, sanitize to slug
    source = (context or task_result)[:200]
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:64] if slug else f"auto-skill-{uuid.uuid4().hex[:8]}"


# ─── Singleton ────────────────────────────────────────────────────────
_singleton: Optional["AutoSkillCreator"] = None
_singleton_lock = threading.Lock()


def get_skill_creator() -> "AutoSkillCreator":
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = AutoSkillCreator()
    return _singleton


# ─── Core class ───────────────────────────────────────────────────────


class AutoSkillCreator:
    """
    Auto Skill Creator — manages reusable skills in SQLite with FTS5 search.

    Usage:
        creator = get_skill_creator()
        skill_id = creator.create_skill("my-skill", "# My Skill\nDo stuff")
        creator.use_skill(skill_id)
        results = creator.search_skills("my skill")
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or str(_db_path())
        self._local = threading.local()
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
            CREATE TABLE IF NOT EXISTS skills (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                content     TEXT NOT NULL DEFAULT '',
                state       TEXT NOT NULL DEFAULT 'active',
                pinned      INTEGER NOT NULL DEFAULT 0,
                use_count   INTEGER NOT NULL DEFAULT 0,
                last_used   TEXT,
                created_by  TEXT NOT NULL DEFAULT 'agent',
                category    TEXT NOT NULL DEFAULT '',
                tags        TEXT NOT NULL DEFAULT '[]',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);
            CREATE INDEX IF NOT EXISTS idx_skills_state ON skills(state);
            CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);

            -- FTS5 virtual table for full-text search on name + content + tags
            CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
                name,
                content,
                tags,
                category,
                content='skills',
                content_rowid='rowid'
            );

            -- Triggers to keep FTS in sync with main table
            CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
                INSERT INTO skills_fts(rowid, name, content, tags, category)
                VALUES (new.rowid, new.name, new.content, new.tags, new.category);
            END;

            CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
                INSERT INTO skills_fts(skills_fts, rowid, name, content, tags, category)
                VALUES ('delete', old.rowid, old.name, old.content, old.tags, old.category);
            END;

            CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills BEGIN
                INSERT INTO skills_fts(skills_fts, rowid, name, content, tags, category)
                VALUES ('delete', old.rowid, old.name, old.content, old.tags, old.category);
                INSERT INTO skills_fts(rowid, name, content, tags, category)
                VALUES (new.rowid, new.name, new.content, new.tags, new.category);
            END;
            """
        )
        conn.commit()

    # ── CRUD ──────────────────────────────────────────────────────────

    def create_skill(
        self,
        name: str,
        content: str,
        category: str = "",
        tags: Optional[List[str]] = None,
        created_by: CreatedBy = CreatedBy.AGENT,
    ) -> str:
        """Create a new skill and return its id."""
        skill_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        tags_json = json.dumps(tags or [])
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO skills
               (id, name, content, state, pinned, use_count, last_used,
                created_by, category, tags, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, 0, NULL, ?, ?, ?, ?, ?)""",
            (
                skill_id,
                name,
                content,
                SkillState.ACTIVE.value,
                created_by.value,
                category,
                tags_json,
                now,
                now,
            ),
        )
        conn.commit()
        logger.info("Created skill %s (%s)", skill_id, name)
        return skill_id

    def get_skill(self, skill_id: str) -> Optional[Skill]:
        """Retrieve a skill by id."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM skills WHERE id = ?", (skill_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_skill(row)

    def update_skill(self, skill_id: str, **fields: Any) -> bool:
        """Update arbitrary fields on a skill. Returns True if updated."""
        allowed = {
            "name",
            "content",
            "state",
            "pinned",
            "category",
            "tags",
        }
        updates = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "state" and isinstance(v, SkillState):
                v = v.value
            if k == "pinned":
                v = int(v)
            if k == "tags" and isinstance(v, list):
                v = json.dumps(v)
            updates[k] = v
        if not updates:
            return False
        updates["updated_at"] = _now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [skill_id]
        conn = self._get_conn()
        cur = conn.execute(
            f"UPDATE skills SET {set_clause} WHERE id = ?", values
        )
        conn.commit()
        return cur.rowcount > 0

    def use_skill(self, skill_id: str) -> bool:
        """Increment use_count and update last_used timestamp."""
        now = _now_iso()
        conn = self._get_conn()
        cur = conn.execute(
            """UPDATE skills
               SET use_count = use_count + 1, last_used = ?, updated_at = ?
               WHERE id = ?""",
            (now, now, skill_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def archive_skill(self, skill_id: str) -> bool:
        """Move a skill to ARCHIVED state."""
        return self.update_skill(skill_id, state=SkillState.ARCHIVED)

    def delete_skill(self, skill_id: str) -> bool:
        """Permanently delete a skill."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        conn.commit()
        if cur.rowcount > 0:
            logger.info("Deleted skill %s", skill_id)
        return cur.rowcount > 0

    # ── Search ────────────────────────────────────────────────────────

    def search_skills(
        self,
        query: str,
        category: Optional[str] = None,
        state: Optional[SkillState] = None,
        limit: int = 10,
    ) -> List[Skill]:
        """FTS5 full-text search across skill name, content, tags, and category."""
        conn = self._get_conn()

        # Sanitize query for FTS5: strip special chars, join with OR
        clean = re.sub(r"[^\w\s\-]", " ", query)
        tokens = clean.split()
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in tokens if t)

        sql = """
            SELECT s.* FROM skills s
            JOIN skills_fts f ON s.rowid = f.rowid
            WHERE skills_fts MATCH ?
        """
        params: List[Any] = [fts_query]

        if category:
            sql += " AND s.category = ?"
            params.append(category)
        if state:
            sql += " AND s.state = ?"
            params.append(state.value)

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_skill(r) for r in rows]

    # ── Stats ─────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Aggregate statistics about all skills."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
        by_state = {}
        for row in conn.execute(
            "SELECT state, COUNT(*) as cnt FROM skills GROUP BY state"
        ):
            by_state[row["state"]] = row["cnt"]
        pinned = conn.execute(
            "SELECT COUNT(*) FROM skills WHERE pinned = 1"
        ).fetchone()[0]
        total_uses = conn.execute(
            "SELECT COALESCE(SUM(use_count), 0) FROM skills"
        ).fetchone()[0]
        by_category = {}
        for row in conn.execute(
            "SELECT category, COUNT(*) as cnt FROM skills GROUP BY category"
        ):
            by_category[row["category"]] = row["cnt"]
        return {
            "total": total,
            "by_state": by_state,
            "pinned": pinned,
            "total_uses": total_uses,
            "by_category": by_category,
        }

    # ── Auto-creation hook ────────────────────────────────────────────

    def after_task_hook(
        self,
        task_result: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        context: Optional[str] = None,
        duration_seconds: Optional[float] = None,
    ) -> Optional[str]:
        """
        Analyze a completed task and optionally create a reusable skill.

        Creates a skill if the task was complex (>3 tool calls or >5 min).
        Returns the new skill id if created, None otherwise.
        """
        tool_calls = tool_calls or []
        if len(tool_calls) <= 3 and (duration_seconds or 0) <= 300:
            logger.debug(
                "Task not complex enough for auto-skill (%d calls, %.0fs)",
                len(tool_calls),
                duration_seconds or 0,
            )
            return None

        # Derive name
        name = _skill_name_from_task(task_result, context or "")

        # Build SKILL.md content
        tags = _tag_extract(tool_calls)
        tool_names = [tc.get("name", tc.get("tool", "unknown")) for tc in tool_calls]

        content_parts = [
            f"# Auto-generated skill: {name}",
            "",
            "## Trigger",
            f"Task completed with {len(tool_calls)} tool calls",
            f"Tools used: {', '.join(tool_names)}",
            "",
            "## Procedure",
        ]

        # Include tool call sequence as procedure
        for i, tc in enumerate(tool_calls, 1):
            tc_name = tc.get("name", tc.get("tool", "unknown"))
            tc_input = tc.get("input", tc.get("args", {}))
            if isinstance(tc_input, dict):
                input_summary = json.dumps(tc_input, default=str)[:200]
            else:
                input_summary = str(tc_input)[:200]
            content_parts.append(f"{i}. `{tc_name}` — {input_summary}")

        content_parts.extend(
            [
                "",
                "## Result",
                (task_result or "No result recorded")[:500],
                "",
                "---",
                f"_Auto-created {datetime.now(timezone.utc).isoformat()}_",
            ]
        )

        content = "\n".join(content_parts)

        skill_id = self.create_skill(
            name=name,
            content=content,
            category="auto-generated",
            tags=tags,
            created_by=CreatedBy.AGENT,
        )
        logger.info(
            "Auto-created skill %s from %d tool calls", skill_id, len(tool_calls)
        )
        return skill_id

    # ── Internal helpers ──────────────────────────────────────────────

    def _row_to_skill(self, row: sqlite3.Row) -> Skill:
        """Convert a DB row to a Skill dataclass."""
        tags = json.loads(row["tags"]) if row["tags"] else []
        return Skill(
            id=row["id"],
            name=row["name"],
            content=row["content"],
            state=SkillState(row["state"]),
            pinned=bool(row["pinned"]),
            use_count=row["use_count"],
            last_used=row["last_used"],
            created_by=CreatedBy(row["created_by"]),
            category=row["category"],
            tags=tags,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
