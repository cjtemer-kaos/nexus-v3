"""
Learning Graph Visualization for SuperNEXUS v2.

Tracks skills, concepts, tasks, and achievements as nodes in a graph.
Edges capture prerequisite, builds-on, and related-to relationships.
Backed by SQLite for persistence.

Usage:
    from src.core.learning_graph import get_learning_graph, NodeType, RelationshipType

    lg = get_learning_graph()
    node_id = lg.add_node("Python Basics", NodeType.SKILL, level=3)
    node_id2 = lg.add_node("Async Programming", NodeType.SKILL)
    lg.add_edge(node_id, node_id2, RelationshipType.PREREQUISITE)
    lg.unlock_achievement("First Steps", "Created first skill node")
    report = lg.get_progress()
    print(lg.export_mermaid())
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".nexus" / "brain" / "learning_graph.db"


# =============================================================================
# Enums
# =============================================================================

class NodeType(Enum):
    """Type of node in the learning graph."""
    SKILL = "SKILL"
    CONCEPT = "CONCEPT"
    TASK = "TASK"
    ACHIEVEMENT = "ACHIEVEMENT"


class RelationshipType(Enum):
    """Type of edge relationship between nodes."""
    PREREQUISITE = "PREREQUISITE"
    BUILDS_ON = "BUILDS_ON"
    RELATED_TO = "RELATED_TO"


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class Node:
    """A node in the learning graph."""
    id: int = 0
    label: str = ""
    type: NodeType = NodeType.SKILL
    level: int = 1
    experience: int = 0
    unlocked_at: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "type": self.type.value,
            "level": self.level, "experience": self.experience,
            "unlocked_at": self.unlocked_at, "description": self.description,
        }


@dataclass
class Edge:
    """An edge in the learning graph."""
    from_id: int = 0
    to_id: int = 0
    relationship: RelationshipType = RelationshipType.RELATED_TO
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_id": self.from_id, "to_id": self.to_id,
            "relationship": self.relationship.value, "weight": self.weight,
        }


@dataclass
class ProgressReport:
    """Aggregated progress report from the learning graph."""
    total_nodes: int = 0
    total_edges: int = 0
    total_achievements: int = 0
    level_distribution: dict[int, int] = field(default_factory=dict)
    recent_unlocks: list[dict[str, Any]] = field(default_factory=list)
    nodes_by_type: dict[str, int] = field(default_factory=dict)
    average_level: float = 0.0
    total_experience: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_nodes": self.total_nodes, "total_edges": self.total_edges,
            "total_achievements": self.total_achievements,
            "level_distribution": self.level_distribution,
            "recent_unlocks": self.recent_unlocks,
            "nodes_by_type": self.nodes_by_type,
            "average_level": self.average_level,
            "total_experience": self.total_experience,
        }


# =============================================================================
# Legacy backward-compat dataclasses
# =============================================================================

@dataclass
class LearningEvent:
    """Legacy learning event (backward compatibility)."""
    id: Optional[int] = None
    event_type: str = ""
    category: str = ""
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "event_type": self.event_type,
            "category": self.category, "description": self.description,
            "metadata": self.metadata, "timestamp": self.timestamp,
        }


@dataclass
class SkillInfo:
    """Legacy skill info for backward compatibility."""
    name: str = ""
    category: str = ""
    use_count: int = 0
    proficiency: float = 0.0


@dataclass
class LegacyStats:
    """Legacy stats for backward compatibility."""
    total_events: int = 0
    total_skills: int = 0
    streak_days: int = 0
    events_by_type: dict[str, int] = field(default_factory=dict)


# =============================================================================
# Singleton
# =============================================================================

_instance: Optional["LearningGraph"] = None


def get_learning_graph(db_path: Optional[str | Path] = None) -> "LearningGraph":
    """Get or create the singleton LearningGraph instance."""
    global _instance
    if _instance is None:
        _instance = LearningGraph(db_path=db_path or DB_PATH)
    return _instance


def reset_singleton() -> None:
    """Reset the singleton (for testing)."""
    global _instance
    _instance = None


# =============================================================================
# LearningGraph
# =============================================================================

class LearningGraph:
    """
    Learning Graph backed by SQLite.

    Stores nodes (skills, concepts, tasks, achievements) and edges
    (relationships between them). Connections are created fresh per
    operation and closed immediately — no persistent file locks.
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # -------------------------------------------------------------------------
    # Database lifecycle (fresh connection per operation)
    # -------------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Create a fresh SQLite connection."""
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _db(self):
        """Context manager yielding a connection that auto-closes."""
        conn = self._get_conn()
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        with self._db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    label       TEXT NOT NULL,
                    node_type   TEXT NOT NULL DEFAULT 'SKILL',
                    level       INTEGER NOT NULL DEFAULT 1 CHECK(level BETWEEN 1 AND 10),
                    experience  INTEGER NOT NULL DEFAULT 0,
                    description TEXT NOT NULL DEFAULT '',
                    unlocked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edges (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_id       INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                    to_id         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                    relationship  TEXT NOT NULL DEFAULT 'RELATED_TO',
                    weight        REAL NOT NULL DEFAULT 1.0,
                    UNIQUE(from_id, to_id, relationship)
                );
                CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
                CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
                CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
            """)
            _ensure_legacy_tables(conn)

    def close(self) -> None:
        """No-op: connections are not cached."""

    def __del__(self):
        """No-op: connections are not cached."""

    # -------------------------------------------------------------------------
    # Node operations
    # -------------------------------------------------------------------------

    def add_node(
        self,
        label: str,
        node_type: NodeType | str = NodeType.SKILL,
        level: int = 1,
        experience: int = 0,
        description: str = "",
    ) -> int:
        """Add a node. Returns the new node's ID."""
        if isinstance(node_type, str):
            node_type = NodeType(node_type.upper())
        if not 1 <= level <= 10:
            raise ValueError(f"Level must be between 1 and 10, got {level}")

        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            cur = conn.execute(
                "INSERT INTO nodes (label, node_type, level, experience, description, unlocked_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (label, node_type.value, level, experience, description, now),
            )
            conn.commit()
            node_id = cur.lastrowid
        logger.info("Added node %d: %s (%s)", node_id, label, node_type.value)
        return node_id

    def get_node(self, node_id: int) -> Optional[Node]:
        """Retrieve a node by ID."""
        with self._db() as conn:
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    def update_node_level(self, node_id: int, level: int) -> bool:
        """Update a node's level."""
        if not 1 <= level <= 10:
            raise ValueError(f"Level must be between 1 and 10, got {level}")
        with self._db() as conn:
            cur = conn.execute("UPDATE nodes SET level = ? WHERE id = ?", (level, node_id))
            conn.commit()
        return cur.rowcount > 0

    def add_experience(self, node_id: int, xp: int) -> bool:
        """Add experience points to a node."""
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE nodes SET experience = experience + ? WHERE id = ?", (xp, node_id)
            )
            conn.commit()
        return cur.rowcount > 0

    def delete_node(self, node_id: int) -> bool:
        """Delete a node and its edges."""
        with self._db() as conn:
            cur = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            conn.commit()
        return cur.rowcount > 0

    def list_nodes(self, node_type: Optional[NodeType | str] = None) -> list[Node]:
        """List all nodes, optionally filtered by type."""
        with self._db() as conn:
            if node_type is not None:
                if isinstance(node_type, str):
                    node_type = NodeType(node_type.upper())
                rows = conn.execute(
                    "SELECT * FROM nodes WHERE node_type = ? ORDER BY id", (node_type.value,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
        return [self._row_to_node(r) for r in rows]

    # -------------------------------------------------------------------------
    # Edge operations
    # -------------------------------------------------------------------------

    def add_edge(
        self,
        from_id: int,
        to_id: int,
        relationship: RelationshipType | str = RelationshipType.RELATED_TO,
        weight: float = 1.0,
    ) -> bool:
        """Add an edge. Returns True if created, False if duplicate."""
        if isinstance(relationship, str):
            relationship = RelationshipType(relationship.upper())
        with self._db() as conn:
            try:
                conn.execute(
                    "INSERT INTO edges (from_id, to_id, relationship, weight) VALUES (?, ?, ?, ?)",
                    (from_id, to_id, relationship.value, weight),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_edges(self, node_id: Optional[int] = None) -> list[Edge]:
        """Get all edges, optionally filtered by node."""
        with self._db() as conn:
            if node_id is not None:
                rows = conn.execute(
                    "SELECT * FROM edges WHERE from_id = ? OR to_id = ? ORDER BY id",
                    (node_id, node_id),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM edges ORDER BY id").fetchall()
        return [self._row_to_edge(r) for r in rows]

    def delete_edge(self, from_id: int, to_id: int, relationship: RelationshipType | str) -> bool:
        """Delete a specific edge."""
        if isinstance(relationship, str):
            relationship = RelationshipType(relationship.upper())
        with self._db() as conn:
            cur = conn.execute(
                "DELETE FROM edges WHERE from_id = ? AND to_id = ? AND relationship = ?",
                (from_id, to_id, relationship.value),
            )
            conn.commit()
        return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Achievements
    # -------------------------------------------------------------------------

    def unlock_achievement(self, label: str, description: str = "") -> int:
        """Unlock an achievement node. Returns the node ID."""
        node_id = self.add_node(
            label=label, node_type=NodeType.ACHIEVEMENT,
            level=10, experience=0, description=description,
        )
        logger.info("Achievement unlocked: %s (node %d)", label, node_id)
        return node_id

    def get_achievements(self) -> list[Node]:
        """Get all achievement nodes."""
        return self.list_nodes(NodeType.ACHIEVEMENT)

    # -------------------------------------------------------------------------
    # Progress & Stats
    # -------------------------------------------------------------------------

    def get_progress(self) -> ProgressReport:
        """Generate an aggregated progress report."""
        with self._db() as conn:
            total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            total_achievements = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE node_type = 'ACHIEVEMENT'"
            ).fetchone()[0]

            level_dist: dict[int, int] = {}
            for row in conn.execute(
                "SELECT level, COUNT(*) as cnt FROM nodes GROUP BY level ORDER BY level"
            ).fetchall():
                level_dist[row["level"]] = row["cnt"]

            nodes_by_type: dict[str, int] = {}
            for row in conn.execute(
                "SELECT node_type, COUNT(*) as cnt FROM nodes GROUP BY node_type"
            ).fetchall():
                nodes_by_type[row["node_type"]] = row["cnt"]

            avg_row = conn.execute("SELECT AVG(level) FROM nodes").fetchone()
            avg_level = float(avg_row[0]) if avg_row[0] else 0.0

            xp_row = conn.execute("SELECT COALESCE(SUM(experience), 0) FROM nodes").fetchone()
            total_xp = int(xp_row[0])

            recent_rows = conn.execute(
                "SELECT id, label, description, unlocked_at FROM nodes "
                "WHERE node_type = 'ACHIEVEMENT' ORDER BY unlocked_at DESC LIMIT 10"
            ).fetchall()
            recent_unlocks = [dict(r) for r in recent_rows]

        return ProgressReport(
            total_nodes=total_nodes, total_edges=total_edges,
            total_achievements=total_achievements,
            level_distribution=level_dist, recent_unlocks=recent_unlocks,
            nodes_by_type=nodes_by_type, average_level=round(avg_level, 2),
            total_experience=total_xp,
        )

    def get_skill_tree(self) -> dict[str, Any]:
        """Return the full skill tree as a nested dict structure."""
        with self._db() as conn:
            nodes = [self._row_to_node(r).to_dict()
                     for r in conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()]
            edges = [self._row_to_edge(r).to_dict()
                     for r in conn.execute("SELECT * FROM edges ORDER BY id").fetchall()]

        incoming = {e["to_id"] for e in edges}
        roots = [n["id"] for n in nodes if n["id"] not in incoming]

        children_map: dict[int, list[int]] = defaultdict(list)
        edge_rel_map: dict[tuple[int, int], str] = {}
        for e in edges:
            children_map[e["from_id"]].append(e["to_id"])
            edge_rel_map[(e["from_id"], e["to_id"])] = e["relationship"]

        node_map = {n["id"]: n for n in nodes}

        def build_subtree(nid: int, visited: set[int]) -> dict[str, Any]:
            visited.add(nid)
            children = []
            for cid in children_map.get(nid, []):
                if cid not in visited:
                    children.append(build_subtree(cid, visited))
            rel = ""
            if children:
                first_cid = children[0]["node"]["id"]
                rel = edge_rel_map.get((nid, first_cid), "")
            return {"node": node_map.get(nid, {}), "relationship_to_parent": rel, "children": children}

        tree: list[dict[str, Any]] = []
        visited: set[int] = set()
        for rid in roots:
            tree.append(build_subtree(rid, visited))
        for n in nodes:
            if n["id"] not in visited:
                tree.append({"node": n, "relationship_to_parent": "", "children": []})

        return {"nodes": nodes, "edges": edges, "roots": roots, "tree": tree}

    def get_stats(self, days: int = 30) -> LegacyStats:
        """Return stats. LegacyStats object for backward compatibility."""
        return _get_legacy_stats(self, days)

    # -------------------------------------------------------------------------
    # Mermaid export
    # -------------------------------------------------------------------------

    def export_mermaid(self) -> str:
        """Generate a Mermaid graph diagram string."""
        with self._db() as conn:
            nodes = conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
            edges = conn.execute("SELECT * FROM edges ORDER BY id").fetchall()

        shape_map = {
            "SKILL": ("([", "])"),
            "CONCEPT": ("{{", "}}"),
            "TASK": ("[", "]"),
            "ACHIEVEMENT": (">(", ")<"),
        }
        rel_style = {
            "PREREQUISITE": "-->|",
            "BUILDS_ON": "-.->|",
            "RELATED_TO": "---|",
        }

        lines = ["graph LR"]
        for row in nodes:
            nid, label, ntype, level = row["id"], row["label"].replace('"', "'"), row["node_type"], row["level"]
            left, right = shape_map.get(ntype, ("[", "]"))
            lines.append(f'    n{nid}{left}"{label}\\nLv.{level}"{right}')

        for row in edges:
            prefix = rel_style.get(row["relationship"], "---|")
            lines.append(f"    n{row['from_id']}{prefix}{row['relationship']}| n{row['to_id']}")

        achievement_ids = [row["id"] for row in nodes if row["node_type"] == "ACHIEVEMENT"]
        if achievement_ids:
            lines.append("    classDef achievement fill:#f9f,stroke:#333,stroke-width:2px")
            lines.append(f"    class {','.join(f'n{i}' for i in achievement_ids)} achievement")

        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> Node:
        return Node(
            id=row["id"], label=row["label"], type=NodeType(row["node_type"]),
            level=row["level"], experience=row["experience"],
            unlocked_at=row["unlocked_at"], description=row["description"],
        )

    @staticmethod
    def _row_to_edge(row: sqlite3.Row) -> Edge:
        return Edge(
            from_id=row["from_id"], to_id=row["to_id"],
            relationship=RelationshipType(row["relationship"]), weight=row["weight"],
        )

    def __repr__(self) -> str:
        stats = self.get_stats()
        return (
            f"LearningGraph(nodes={stats['total_nodes']}, "
            f"edges={stats['total_edges']}, "
            f"achievements={stats['total_achievements']}, "
            f"db={self.db_path})"
        )


# =============================================================================
# Legacy backward-compatible methods (old LearningEvent-based API)
# =============================================================================

def _ensure_legacy_tables(conn: sqlite3.Connection) -> None:
    """Create legacy tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS legacy_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            metadata    TEXT NOT NULL DEFAULT '{}',
            timestamp   TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS legacy_skills (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL UNIQUE,
            category  TEXT NOT NULL DEFAULT '',
            use_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


def _record_event(self, event_type: str, category: str, description: str) -> int:
    """Legacy: record a learning event."""
    with self._db() as conn:
        _ensure_legacy_tables(conn)
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO legacy_events (event_type, category, description, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (event_type, category, description, now),
        )
        conn.commit()
        return cur.lastrowid


def _record_task(self, description: str, success: bool = True, duration_seconds: float = 0) -> int:
    """Legacy: record a task completion."""
    return self.record_event("task_completed", "coding", description)


def _record_skill_use(self, skill_name: str, category: str = "") -> None:
    """Legacy: record skill usage, incrementing use count."""
    with self._db() as conn:
        _ensure_legacy_tables(conn)
        existing = conn.execute(
            "SELECT id, use_count FROM legacy_skills WHERE name = ?", (skill_name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE legacy_skills SET use_count = use_count + 1 WHERE id = ?",
                (existing["id"],),
            )
        else:
            conn.execute(
                "INSERT INTO legacy_skills (name, category, use_count) VALUES (?, ?, 1)",
                (skill_name, category),
            )
        conn.commit()


def _get_skills(self) -> list[SkillInfo]:
    """Legacy: get all tracked skills."""
    with self._db() as conn:
        _ensure_legacy_tables(conn)
        rows = conn.execute("SELECT name, category, use_count FROM legacy_skills ORDER BY name").fetchall()
    return [
        SkillInfo(
            name=r["name"], category=r["category"],
            use_count=r["use_count"],
            proficiency=round(min(1.0, r["use_count"] / 20.0), 2),
        )
        for r in rows
    ]


def _get_legacy_stats(self, days: int = 30) -> LegacyStats:
    """Legacy: get stats."""
    with self._db() as conn:
        _ensure_legacy_tables(conn)
        total_events = conn.execute("SELECT COUNT(*) FROM legacy_events").fetchone()[0]
        total_skills = conn.execute("SELECT COUNT(*) FROM legacy_skills").fetchone()[0]

        rows = conn.execute(
            "SELECT DISTINCT substr(timestamp, 1, 10) as day FROM legacy_events "
            "ORDER BY day DESC LIMIT ?", (days,)
        ).fetchall()
        streak = len(rows)

        by_type: dict[str, int] = {}
        for r in conn.execute(
            "SELECT event_type, COUNT(*) as cnt FROM legacy_events GROUP BY event_type"
        ).fetchall():
            by_type[r["event_type"]] = r["cnt"]

    return LegacyStats(
        total_events=total_events, total_skills=total_skills,
        streak_days=streak, events_by_type=by_type,
    )


def _get_timeline(self, days: int = 7) -> list[dict[str, Any]]:
    """Legacy: get recent events as timeline."""
    with self._db() as conn:
        _ensure_legacy_tables(conn)
        rows = conn.execute(
            "SELECT event_type, category, description, timestamp FROM legacy_events "
            "ORDER BY timestamp DESC LIMIT 100"
        ).fetchall()
    return [dict(r) for r in rows]


# Monkey-patch legacy methods onto LearningGraph
LearningGraph.record_event = _record_event
LearningGraph.record_task = _record_task
LearningGraph.record_skill_use = _record_skill_use
LearningGraph.get_skills = _get_skills
LearningGraph.get_timeline = _get_timeline
