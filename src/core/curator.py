"""
Background Curator — Skill quality maintenance and consolidation (inspired by Hermes curator)

Features:
  - Duplicate skill detection (name similarity + content overlap)
  - Consolidation suggestions and merge operations
  - Backup/rollback for safe skill modifications
  - Prefix group analysis for umbrella consolidation
  - CuratorReport for structured reporting

Patrons:
  - Hermes curator (skill_manage absorbed_into pattern)
  - skill_lifecycle.py (shared lifecycle awareness)
  - auto_skill_creator.py (skill data model)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nexus-curator")

# ─── Helpers ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    home = Path.home()
    db_dir = home / ".nexus" / "brain"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "curator.db"


def _backup_dir() -> Path:
    d = Path.home() / ".nexus" / "brain" / "curator_backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _content_hash(content: str) -> str:
    """SHA256 hash of content for duplicate detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _name_tokens(name: str) -> set:
    """Extract lowercase alphanumeric tokens from a name for similarity."""
    return set(re.findall(r"[a-z0-9]+", name.lower()))


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ─── Dataclass ────────────────────────────────────────────────────────


@dataclass
class SkillInfo:
    """Minimal skill info for curator operations (reads from lifecycle DB or auto_skill_creator DB)."""

    id: str
    name: str
    content: str
    state: str = "active"
    pinned: bool = False
    use_count: int = 0
    last_used: Optional[str] = None
    created_at: str = ""
    category: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass
class ConsolidationPlan:
    """A plan to merge multiple skills into one."""

    skill_ids: List[str]
    skill_names: List[str]
    suggested_name: str
    reason: str
    estimated_content_size: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CuratorReport:
    """Summary report from a curator run."""

    duplicates_found: int = 0
    consolidation_suggestions: int = 0
    skills_merged: int = 0
    skills_rolled_back: int = 0
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── Singleton ────────────────────────────────────────────────────────
_singleton: Optional["Curator"] = None
_singleton_lock = threading.Lock()


def get_curator() -> "Curator":
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = Curator()
    return _singleton


# ─── Core class ───────────────────────────────────────────────────────


class Curator:
    """
    Background Curator — maintains skill quality through dedup, consolidation, and rollback.

    Usage:
        curator = get_curator()
        dupes = curator.find_duplicate_skills()
        plans = curator.suggest_consolidation()
        backup = curator.backup_skill(skill_id)
        curator.rollback_skill(skill_id, backup)
        merged = curator.merge_skills([id1, id2], "new-name", "merged content")
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

            CREATE TABLE IF NOT EXISTS backups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id    TEXT NOT NULL,
                backup_path TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS merge_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_ids      TEXT NOT NULL,
                new_skill_id    TEXT NOT NULL,
                new_name        TEXT NOT NULL,
                merged_at       TEXT NOT NULL
            );
            """
        )
        conn.commit()

    # ── Sync skills from external stores ──────────────────────────────

    def sync_from_creator(self) -> int:
        """
        Pull skills from auto_skill_creator DB into curator DB.

        Returns count of skills synced.
        """
        creator_db = Path.home() / ".nexus" / "brain" / "skills_creator.db"
        if not creator_db.exists():
            return 0

        try:
            src = sqlite3.connect(str(creator_db))
            src.row_factory = sqlite3.Row
            rows = src.execute("SELECT * FROM skills").fetchall()
            src.close()
        except Exception as exc:
            logger.warning("Failed to read creator DB: %s", exc)
            return 0

        conn = self._get_conn()
        now = _now_iso()
        count = 0
        for row in rows:
            conn.execute(
                """INSERT OR REPLACE INTO skills
                   (id, name, content, state, pinned, use_count,
                    last_used, created_by, category, tags, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"],
                    row["name"],
                    row["content"],
                    row["state"],
                    row["pinned"],
                    row["use_count"],
                    row["last_used"],
                    row["created_by"],
                    row["category"],
                    row["tags"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
            count += 1
        conn.commit()
        logger.info("Synced %d skills from creator DB", count)
        return count

    def sync_from_lifecycle(self) -> int:
        """
        Pull skills from skill_lifecycle DB into curator DB (state + pinned info).
        """
        lc_db = Path.home() / ".nexus" / "brain" / "skill_lifecycle.db"
        if not lc_db.exists():
            return 0

        try:
            src = sqlite3.connect(str(lc_db))
            src.row_factory = sqlite3.Row
            rows = src.execute("SELECT * FROM skill_lifecycle").fetchall()
            src.close()
        except Exception as exc:
            logger.warning("Failed to read lifecycle DB: %s", exc)
            return 0

        conn = self._get_conn()
        now = _now_iso()
        count = 0
        for row in rows:
            # Update only lifecycle-relevant fields
            cur = conn.execute(
                """UPDATE skills SET state = ?, pinned = ?, last_used = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    row["state"],
                    row["pinned"],
                    row["last_used"],
                    now,
                    row["skill_id"],
                ),
            )
            if cur.rowcount == 0:
                # Skill doesn't exist in curator DB; insert a stub
                conn.execute(
                    """INSERT OR IGNORE INTO skills
                       (id, name, content, state, pinned, created_at, updated_at)
                       VALUES (?, '', '', ?, ?, ?, ?)""",
                    (
                        row["skill_id"],
                        row["state"],
                        row["pinned"],
                        now,
                        now,
                    ),
                )
            count += 1
        conn.commit()
        logger.info("Synced %d skill states from lifecycle DB", count)
        return count

    # ── Duplicate detection ───────────────────────────────────────────

    def find_duplicate_skills(self) -> List[Tuple[SkillInfo, SkillInfo, float]]:
        """
        Find pairs of skills that are likely duplicates.

        Uses content hash and name Jaccard similarity.
        Returns list of (skill_a, skill_b, similarity_score).
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM skills WHERE state != 'archived'"
        ).fetchall()
        skills = [self._row_to_skill(r) for r in rows]

        duplicates: List[Tuple[SkillInfo, SkillInfo, float]] = []

        # Group by content hash for exact duplicates
        by_hash: Dict[str, List[SkillInfo]] = defaultdict(list)
        for s in skills:
            h = _content_hash(s.content)
            by_hash[h].append(s)

        for h, group in by_hash.items():
            if len(group) > 1:
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        duplicates.append((group[i], group[j], 1.0))

        # Fuzzy name similarity for near-duplicates
        seen_pairs = {(a.id, b.id) for a, b, _ in duplicates}
        for i in range(len(skills)):
            for j in range(i + 1, len(skills)):
                a, b = skills[i], skills[j]
                pair_key = (min(a.id, b.id), max(a.id, b.id))
                if pair_key in seen_pairs:
                    continue
                tokens_a = _name_tokens(a.name)
                tokens_b = _name_tokens(b.name)
                sim = _jaccard(tokens_a, tokens_b)
                # Also consider content similarity
                if a.content and b.content:
                    ct_a = _name_tokens(a.content[:500])
                    ct_b = _name_tokens(b.content[:500])
                    content_sim = _jaccard(ct_a, ct_b)
                    sim = sim * 0.6 + content_sim * 0.4

                if sim >= 0.7:
                    duplicates.append((a, b, round(sim, 3)))
                    seen_pairs.add(pair_key)

        logger.info("Found %d duplicate pairs", len(duplicates))
        return duplicates

    # ── Consolidation suggestions ─────────────────────────────────────

    def suggest_consolidation(self) -> List[ConsolidationPlan]:
        """
        Generate consolidation plans from duplicate groups and prefix clusters.

        Returns prioritized list of plans sorted by benefit score.
        """
        plans: List[ConsolidationPlan] = []

        # From exact duplicates
        dupes = self.find_duplicate_skills()
        content_groups: Dict[str, List[SkillInfo]] = defaultdict(list)
        for a, b, sim in dupes:
            if sim >= 0.95:  # near-exact
                content_groups[_content_hash(a.content)].append(a)
                content_groups[_content_hash(b.content)].append(b)

        for h, group in content_groups.items():
            if len(group) < 2:
                continue
            ids = list({s.id for s in group})
            names = list({s.name for s in group})
            total_size = sum(len(s.content) for s in group)
            plans.append(
                ConsolidationPlan(
                    skill_ids=ids,
                    skill_names=names,
                    suggested_name=names[0],
                    reason=f"Exact content duplicate ({len(ids)} copies)",
                    estimated_content_size=total_size,
                )
            )

        # From prefix groups
        prefix_plans = self.analyze_prefix_groups()
        plans.extend(prefix_plans)

        # Sort by estimated benefit (fewer remaining skills = better)
        plans.sort(key=lambda p: -len(p.skill_ids))
        logger.info("Generated %d consolidation plans", len(plans))
        return plans

    def analyze_prefix_groups(self) -> List[ConsolidationPlan]:
        """
        Find skills with similar name prefixes and suggest umbrella consolidation.

        E.g. hermes-config-auth, hermes-config-gateway -> hermes-config
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, name, content FROM skills WHERE state != 'archived'"
        ).fetchall()

        # Extract prefix patterns: look for "word-" or "word_word-"
        prefix_map: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        for row in rows:
            name = row["name"]
            tokens = name.split("-")
            if len(tokens) >= 2:
                prefix = tokens[0]
                prefix_map[prefix].append((row["id"], name, row["content"]))

        plans: List[ConsolidationPlan] = []
        for prefix, group in prefix_map.items():
            if len(group) < 3:  # need at least 3 skills to suggest umbrella
                continue

            ids = [gid for gid, _, _ in group]
            names = [gn for _, gn, _ in group]
            total_size = sum(len(c) for _, _, c in group)
            suggested = prefix  # umbrella name

            plans.append(
                ConsolidationPlan(
                    skill_ids=ids,
                    skill_names=names,
                    suggested_name=suggested,
                    reason=f"Prefix group '{prefix}-*' with {len(group)} skills",
                    estimated_content_size=total_size,
                )
            )

        return plans

    # ── Backup / Rollback ─────────────────────────────────────────────

    def backup_skill(self, skill_id: str) -> Optional[str]:
        """
        Create a JSON backup of a skill.

        Returns the backup file path.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM skills WHERE id = ?", (skill_id,)
        ).fetchone()
        if row is None:
            logger.warning("Skill %s not found for backup", skill_id)
            return None

        backup_data = {
            "id": row["id"],
            "name": row["name"],
            "content": row["content"],
            "state": row["state"],
            "pinned": row["pinned"],
            "use_count": row["use_count"],
            "last_used": row["last_used"],
            "created_by": row["created_by"],
            "category": row["category"],
            "tags": row["tags"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "backup_timestamp": _now_iso(),
        }

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{skill_id}_{ts}.json"
        backup_path = _backup_dir() / filename

        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2, ensure_ascii=False)

        # Record backup in DB
        conn.execute(
            """INSERT INTO backups (skill_id, backup_path, created_at)
               VALUES (?, ?, ?)""",
            (skill_id, str(backup_path), _now_iso()),
        )
        conn.commit()

        logger.info("Backed up skill %s to %s", skill_id, backup_path)
        return str(backup_path)

    def rollback_skill(self, skill_id: str, backup_path: str) -> bool:
        """
        Restore a skill from a backup file.

        Returns True if successful.
        """
        if not os.path.exists(backup_path):
            logger.error("Backup file not found: %s", backup_path)
            return False

        try:
            with open(backup_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as exc:
            logger.error("Failed to read backup %s: %s", backup_path, exc)
            return False

        conn = self._get_conn()
        now = _now_iso()
        conn.execute(
            """INSERT OR REPLACE INTO skills
               (id, name, content, state, pinned, use_count,
                last_used, created_by, category, tags, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["id"],
                data["name"],
                data["content"],
                data.get("state", "active"),
                data.get("pinned", 0),
                data.get("use_count", 0),
                data.get("last_used"),
                data.get("created_by", "agent"),
                data.get("category", ""),
                data.get("tags", "[]"),
                data.get("created_at", now),
                now,
            ),
        )
        conn.commit()

        logger.info("Rolled back skill %s from %s", skill_id, backup_path)
        return True

    # ── Merge ─────────────────────────────────────────────────────────

    def merge_skills(
        self,
        skill_ids: List[str],
        new_name: str,
        new_content: str,
    ) -> Optional[str]:
        """
        Merge multiple skills into one. Backs up originals first.

        Returns the new merged skill id.
        """
        if len(skill_ids) < 2:
            logger.warning("Need at least 2 skills to merge")
            return None

        conn = self._get_conn()

        # Back up all source skills
        for sid in skill_ids:
            self.backup_skill(sid)

        # Archive the source skills
        now = _now_iso()
        for sid in skill_ids:
            conn.execute(
                """UPDATE skills SET state = 'archived', updated_at = ?
                   WHERE id = ?""",
                (now, sid),
            )

        # Create merged skill
        import uuid as _uuid

        new_id = _uuid.uuid4().hex[:12]

        # Collect tags from all sources
        all_tags: set = set()
        for sid in skill_ids:
            row = conn.execute(
                "SELECT tags FROM skills WHERE id = ?", (sid,)
            ).fetchone()
            if row and row["tags"]:
                try:
                    all_tags.update(json.loads(row["tags"]))
                except (json.JSONDecodeError, TypeError):
                    pass

        conn.execute(
            """INSERT INTO skills
               (id, name, content, state, pinned, use_count,
                created_by, category, tags, created_at, updated_at)
               VALUES (?, ?, ?, 'active', 0, 0, 'curator', 'merged', ?, ?, ?)""",
            (
                new_id,
                new_name,
                new_content,
                json.dumps(sorted(all_tags)),
                now,
                now,
            ),
        )

        # Record merge history
        conn.execute(
            """INSERT INTO merge_history
               (source_ids, new_skill_id, new_name, merged_at)
               VALUES (?, ?, ?, ?)""",
            (json.dumps(skill_ids), new_id, new_name, now),
        )

        conn.commit()
        logger.info(
            "Merged %d skills into %s (%s)", len(skill_ids), new_id, new_name
        )
        return new_id

    # ── Stats ─────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Aggregate curator statistics."""
        conn = self._get_conn()
        total_skills = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
        total_backups = conn.execute("SELECT COUNT(*) FROM backups").fetchone()[0]
        total_merges = conn.execute("SELECT COUNT(*) FROM merge_history").fetchone()[0]

        # Recent backups
        recent_backups = []
        for row in conn.execute(
            """SELECT * FROM backups ORDER BY created_at DESC LIMIT 5"""
        ):
            recent_backups.append(
                {
                    "skill_id": row["skill_id"],
                    "backup_path": row["backup_path"],
                    "created_at": row["created_at"],
                }
            )

        # Recent merges
        recent_merges = []
        for row in conn.execute(
            """SELECT * FROM merge_history ORDER BY merged_at DESC LIMIT 5"""
        ):
            recent_merges.append(
                {
                    "source_ids": json.loads(row["source_ids"]),
                    "new_skill_id": row["new_skill_id"],
                    "new_name": row["new_name"],
                    "merged_at": row["merged_at"],
                }
            )

        return {
            "total_skills": total_skills,
            "total_backups": total_backups,
            "total_merges": total_merges,
            "recent_backups": recent_backups,
            "recent_merges": recent_merges,
        }

    # ── Full curator run ──────────────────────────────────────────────

    def run_full_curate(self) -> CuratorReport:
        """
        Run a full curation pass: sync, detect dupes, suggest consolidation.

        Returns a CuratorReport with all findings.
        """
        report = CuratorReport()

        # Sync from other stores
        synced = self.sync_from_creator()
        report.details.append(f"Synced {synced} skills from creator DB")

        synced_lc = self.sync_from_lifecycle()
        report.details.append(f"Synced {synced_lc} skill states from lifecycle DB")

        # Find duplicates
        dupes = self.find_duplicate_skills()
        report.duplicates_found = len(dupes)
        for a, b, sim in dupes:
            report.details.append(
                f"Duplicate: '{a.name}' ~= '{b.name}' (similarity: {sim:.1%})"
            )

        # Suggest consolidation
        plans = self.suggest_consolidation()
        report.consolidation_suggestions = len(plans)
        for plan in plans:
            report.details.append(
                f"Consolidate: {plan.skill_names} -> '{plan.suggested_name}' "
                f"({plan.reason})"
            )

        logger.info(
            "Curator report: %d dupes, %d suggestions",
            report.duplicates_found,
            report.consolidation_suggestions,
        )
        return report

    # ── Internal helpers ──────────────────────────────────────────────

    def _row_to_skill(self, row: sqlite3.Row) -> SkillInfo:
        """Convert a DB row to a SkillInfo dataclass."""
        tags = []
        if row["tags"]:
            try:
                tags = json.loads(row["tags"])
            except (json.JSONDecodeError, TypeError):
                pass
        return SkillInfo(
            id=row["id"],
            name=row["name"],
            content=row["content"],
            state=row["state"],
            pinned=bool(row["pinned"]),
            use_count=row["use_count"],
            last_used=row["last_used"],
            created_at=row["created_at"],
            category=row["category"],
            tags=tags,
        )
