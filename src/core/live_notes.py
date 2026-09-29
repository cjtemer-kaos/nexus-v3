"""
F20: Live Notes — Auto-updating topic monitoring

Track topics across web sources with auto-update and versioning.
"""

import asyncio
import hashlib
import logging
import re
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nexus-live-notes")


BACKLINK_RE = re.compile(r'\[\[([^\]]+)\]\]')


@dataclass
class LiveNote:
    id: str
    topic: str
    sources: List[str]
    content: str
    last_updated: str = ""
    version: int = 1
    change_count: int = 0
    last_hash: str = ""
    update_interval: int = 300  # seconds
    tags: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.last_updated:
            self.last_updated = datetime.now().isoformat()

    @property
    def outgoing_links(self) -> Set[str]:
        """Extract [[backlinks]] from content (Obsidian pattern)."""
        return set(BACKLINK_RE.findall(self.content))


class LiveNotes:
    """Auto-updating topic monitoring across sources"""

    def __init__(self, notes_path: Optional[str] = None):
        if notes_path is None:
            notes_path = str(Path.home() / ".nexus" / "live_notes")
        self.notes_path = Path(notes_path)
        self.notes_path.mkdir(parents=True, exist_ok=True)
        self._notes: Dict[str, LiveNote] = {}
        self._update_tasks: Dict[str, asyncio.Task] = {}
        self._running = False

    def create_note(self, topic: str, sources: List[str], content: str = "", update_interval: int = 300) -> LiveNote:
        import uuid
        note = LiveNote(
            id=str(uuid.uuid4())[:8],
            topic=topic,
            sources=sources,
            content=content,
            update_interval=update_interval,
        )
        self._notes[note.id] = note
        self._save_note(note)
        logger.info(f"Live note created: {topic} ({note.id})")
        return note

    def update_note(self, note_id: str, new_content: str):
        note = self._notes.get(note_id)
        if not note:
            return

        new_hash = hashlib.md5(new_content.encode()).hexdigest()
        if new_hash != note.last_hash:
            note.version += 1
            note.change_count += 1
            note.content = new_content
            note.last_hash = new_hash
            note.last_updated = datetime.now().isoformat()
            self._save_note(note)
            logger.info(f"Live note updated: {note.topic} (v{note.version})")

    def _save_note(self, note: LiveNote):
        path = self.notes_path / f"{note.id}.md"
        path.write_text(
            f"# {note.topic}\n\n"
            f"**Sources**: {', '.join(note.sources)}\n"
            f"**Updated**: {note.last_updated}\n"
            f"**Version**: {note.version}\n\n"
            f"{note.content}\n",
            encoding="utf-8",
        )

    def get_note(self, note_id: str) -> Optional[LiveNote]:
        return self._notes.get(note_id)

    def list_notes(self) -> List[Dict]:
        return [
            {
                "id": n.id,
                "topic": n.topic,
                "sources": n.sources,
                "version": n.version,
                "changes": n.change_count,
                "last_updated": n.last_updated,
            }
            for n in self._notes.values()
        ]

    async def start_monitoring(self, fetch_func=None):
        """Start auto-updating all notes"""
        self._running = True
        for note in self._notes.values():
            task = asyncio.create_task(self._monitor_note(note, fetch_func))
            self._update_tasks[note.id] = task

    async def _monitor_note(self, note: LiveNote, fetch_func=None):
        while self._running:
            if fetch_func:
                try:
                    new_content = await fetch_func(note.sources)
                    if new_content:
                        self.update_note(note.id, new_content)
                except Exception as e:
                    logger.warning(f"Failed to update note {note.topic}: {e}")

            await asyncio.sleep(note.update_interval)

    def stop_monitoring(self):
        self._running = False
        for task in self._update_tasks.values():
            task.cancel()
        self._update_tasks.clear()

    def get_backlinks(self, topic: str) -> List[Dict]:
        """Find all notes that link TO this topic via [[topic]] (Obsidian-style)."""
        topic_lower = topic.lower()
        results = []
        for note in self._notes.values():
            links = {link.lower() for link in note.outgoing_links}
            if topic_lower in links:
                results.append({
                    "id": note.id,
                    "topic": note.topic,
                    "last_updated": note.last_updated,
                })
        return results

    def get_graph(self) -> Dict:
        """Build a link graph of all notes (for visualization)."""
        nodes = []
        edges = []
        topic_to_id = {n.topic.lower(): n.id for n in self._notes.values()}

        for note in self._notes.values():
            nodes.append({"id": note.id, "topic": note.topic, "version": note.version})
            for link in note.outgoing_links:
                target_id = topic_to_id.get(link.lower())
                if target_id:
                    edges.append({"source": note.id, "target": target_id, "label": link})

        return {"nodes": nodes, "edges": edges}

    def search_notes(self, query: str) -> List[Dict]:
        """Search notes by topic or content."""
        q = query.lower()
        return [
            {
                "id": n.id,
                "topic": n.topic,
                "snippet": n.content[:200],
                "version": n.version,
                "last_updated": n.last_updated,
            }
            for n in self._notes.values()
            if q in n.topic.lower() or q in n.content.lower()
        ]

    def get_stats(self) -> Dict:
        total_changes = sum(n.change_count for n in self._notes.values())
        total_links = sum(len(n.outgoing_links) for n in self._notes.values())
        return {
            "total_notes": len(self._notes),
            "total_changes": total_changes,
            "total_backlinks": total_links,
            "monitoring_active": self._running,
            "notes_path": str(self.notes_path),
        }
