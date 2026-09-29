"""
F9: Knowledge Graph Vault

Markdown-based memory with backlinks, Obsidian-compatible, FTS5 search.
"""

import json
import logging
import sqlite3
import re
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nexus-vault")


@dataclass
class VaultNote:
    id: str
    title: str
    content: str
    category: str = "general"
    tags: List[str] = field(default_factory=list)
    backlinks: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_markdown(self) -> str:
        tags_str = ", ".join(f"#{t}" for t in self.tags)
        backlinks_str = "\n\n## Backlinks\n" + "\n".join(f"- [[{b}]]" for b in self.backlinks) if self.backlinks else ""
        return f"# {self.title}\n\n{tags_str}\n\n{self.content}{backlinks_str}"


class KnowledgeVault:
    """Markdown knowledge vault with backlinks and FTS5 search"""

    def __init__(self, vault_path: Optional[str] = None):
        if vault_path is None:
            vault_path = str(Path.home() / ".nexus" / "vault")
        self.vault_path = Path(vault_path)
        self.vault_path.mkdir(parents=True, exist_ok=True)
        (self.vault_path / "notes").mkdir(exist_ok=True)
        self._init_db()

    def _init_db(self):
        self.db_path = self.vault_path / "vault.db"
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            tags TEXT DEFAULT '[]',
            backlinks TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        # FTS5 virtual table
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
            title, content, tags,
            content='notes',
            content_rowid='rowid'
        )""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
            INSERT INTO notes_fts(rowid, title, content, tags)
            VALUES (new.rowid, new.title, new.content, new.tags);
        END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
            INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
            VALUES('delete', old.rowid, old.title, old.content, old.tags);
        END""")
        conn.commit()
        conn.close()

    def add_note(self, title: str, content: str, category: str = "general", tags: List[str] = None) -> VaultNote:
        import uuid
        note = VaultNote(
            id=str(uuid.uuid4())[:8],
            title=title,
            content=content,
            category=category,
            tags=tags or [],
        )

        # Find backlinks
        note.backlinks = self._find_backlinks(content)

        # Save to DB
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("""INSERT OR REPLACE INTO notes (id, title, content, category, tags, backlinks, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (
            note.id, note.title, note.content, note.category,
            json.dumps(note.tags), json.dumps(note.backlinks),
            note.created_at, note.updated_at,
        ))
        conn.commit()
        conn.close()

        # Save as markdown file
        md_path = self.vault_path / "notes" / f"{note.id}.md"
        md_path.write_text(note.to_markdown(), encoding="utf-8")

        logger.info(f"Vault note added: {title} ({note.id})")
        return note

    def _find_backlinks(self, content: str) -> List[str]:
        """Find [[wiki-style]] backlinks in content"""
        return list(set(re.findall(r"\[\[(.+?)\]\]", content)))

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        """Full-text search using FTS5"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        escaped_query = query.replace('"', '""').replace('*', ' ').replace('(', ' ').replace(')', ' ')
        c.execute("""SELECT notes.*, rank FROM notes_fts
            JOIN notes ON notes_fts.rowid = notes.rowid
            WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?""", (escaped_query, top_k))
        results = [dict(r) for r in c.fetchall()]
        conn.close()
        return results

    def get_note(self, note_id: str) -> Optional[VaultNote]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        try:
            tags = json.loads(row["tags"] or "[]")
            backlinks = json.loads(row["backlinks"] or "[]")
        except (json.JSONDecodeError, TypeError):
            tags = []
            backlinks = []
        return VaultNote(
            id=row["id"], title=row["title"], content=row["content"],
            category=row["category"], tags=tags,
            backlinks=backlinks,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def list_notes(self, category: str = None) -> List[Dict]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if category:
            c.execute("SELECT id, title, category, tags, created_at FROM notes WHERE category = ? ORDER BY updated_at DESC", (category,))
        else:
            c.execute("SELECT id, title, category, tags, created_at FROM notes ORDER BY updated_at DESC")
        results = []
        for r in c.fetchall():
            try:
                tags = json.loads(r["tags"] or "[]")
            except (json.JSONDecodeError, TypeError):
                tags = []
            results.append({
                "id": r["id"], "title": r["title"], "category": r["category"],
                "tags": tags, "created_at": r["created_at"],
            })
        conn.close()
        return results

    def get_stats(self) -> Dict:
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM notes")
        total = c.fetchone()[0]
        c.execute("SELECT DISTINCT category FROM notes")
        categories = [r[0] for r in c.fetchall()]
        c.execute("SELECT COUNT(*) FROM notes WHERE updated_at > datetime('now', '-24 hours')")
        recent = c.fetchone()[0]
        conn.close()
        return {
            "total_notes": total,
            "categories": categories,
            "recent_24h": recent,
            "vault_path": str(self.vault_path),
        }

    def add_link(self, from_note_title: str, to_note_title: str, link_type: str = "related"):
        """Add a backlink between notes"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, backlinks FROM notes WHERE title = ?", (to_note_title,))
        target = c.fetchone()
        if target:
            try:
                backlinks = json.loads(target["backlinks"] or "[]")
            except (json.JSONDecodeError, TypeError):
                backlinks = []
            if from_note_title not in backlinks:
                backlinks.append(from_note_title)
                c.execute("UPDATE notes SET backlinks = ?, updated_at = ? WHERE id = ?",
                          (json.dumps(backlinks), datetime.now().isoformat(), target["id"]))
                conn.commit()
        conn.close()

    def get_graph(self) -> Dict:
        """Get knowledge graph as nodes and edges"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, title, category, tags, backlinks FROM notes")
        nodes = []
        edges = []
        for r in c.fetchall():
            try:
                tags = json.loads(r["tags"] or "[]")
                backlinks = json.loads(r["backlinks"] or "[]")
            except (json.JSONDecodeError, TypeError):
                tags = []
                backlinks = []
            nodes.append({"id": r["id"], "title": r["title"], "category": r["category"], "tags": tags})
            for bl in backlinks:
                edges.append({"from": r["title"], "to": bl, "type": "backlink"})
        conn.close()
        return {"nodes": nodes, "edges": edges}

    def close(self):
        pass
