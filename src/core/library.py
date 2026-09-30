"""
Library - SuperNEXUS v2
Vista unificada de todos los recursos: chats, documentos, gallery, research, notas.
"""

from src.core.environment import data_home
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class LibraryItem:
    def __init__(self, data: Dict):
        self.id: str = data.get("id", "")
        self.title: str = data.get("title", "")
        self.type: str = data.get("type", "document")
        self.source: str = data.get("source", "user")
        self.session_id: Optional[str] = data.get("session_id")
        self.created_at: float = data.get("created_at", time.time())
        self.size: int = data.get("size", 0)
        self.tags: List[str] = data.get("tags", [])
        self.path: Optional[str] = data.get("path")
        self.url: Optional[str] = data.get("url")
        self.metadata: Dict = data.get("metadata", {})

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "title": self.title, "type": self.type,
            "source": self.source, "session_id": self.session_id,
            "created_at": self.created_at, "size": self.size,
            "tags": self.tags, "path": self.path, "url": self.url,
            "metadata": self.metadata,
        }


class LibraryManager:
    """Vista unificada de todos los recursos del sistema."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or data_home()
        self._items: List[LibraryItem] = []

    def scan_chats(self) -> List[Dict]:
        """Escanear sesiones de chat"""
        items = []
        sessions_dir = self.data_dir / "sessions"
        if sessions_dir.exists():
            for f in sessions_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    items.append({
                        "id": f.stem,
                        "title": data.get("name", f.stem),
                        "type": "chat",
                        "created_at": data.get("created_at", 0),
                        "size": f.stat().st_size,
                        "tags": [],
                        "metadata": {"message_count": len(data.get("messages", []))},
                    })
                except Exception:
                    pass
        return items

    def scan_documents(self) -> List[Dict]:
        """Escanear documentos"""
        items = []
        docs_dir = self.data_dir / "documents"
        if docs_dir.exists():
            for f in docs_dir.rglob("*"):
                if f.is_file():
                    items.append({
                        "id": str(f.relative_to(docs_dir)),
                        "title": f.name,
                        "type": "document",
                        "created_at": f.stat().st_mtime,
                        "size": f.stat().st_size,
                        "tags": [f.suffix.lstrip(".")],
                        "path": str(f),
                        "metadata": {},
                    })
        return items

    def scan_gallery(self) -> List[Dict]:
        """Escanear imagenes"""
        items = []
        gallery_dir = self.data_dir / "gallery"
        if gallery_dir.exists():
            for f in gallery_dir.rglob("*"):
                if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                    items.append({
                        "id": f.stem,
                        "title": f.name,
                        "type": "image",
                        "created_at": f.stat().st_mtime,
                        "size": f.stat().st_size,
                        "tags": [],
                        "path": str(f),
                        "metadata": {},
                    })
        return items

    def scan_notes(self) -> List[Dict]:
        """Escanear notas"""
        items = []
        notes_file = self.data_dir / "notes" / "notes.json"
        if notes_file.exists():
            try:
                data = json.loads(notes_file.read_text(encoding="utf-8"))
                for n in data.get("notes", []):
                    items.append({
                        "id": n.get("id", ""),
                        "title": n.get("title", "Sin titulo"),
                        "type": "note",
                        "created_at": n.get("created_at", 0),
                        "size": len(n.get("content", "")),
                        "tags": [n.get("label", "")] if n.get("label") else [],
                        "metadata": {"pinned": n.get("pinned", False)},
                    })
            except Exception:
                pass
        return items

    def scan_research(self) -> List[Dict]:
        """Escanear reportes de research"""
        items = []
        research_dir = self.data_dir / "research"
        if research_dir.exists():
            for f in research_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    items.append({
                        "id": f.stem,
                        "title": data.get("title", f.stem),
                        "type": "research",
                        "created_at": data.get("created_at", 0),
                        "size": f.stat().st_size,
                        "tags": data.get("tags", []),
                        "metadata": {"sources": len(data.get("sources", []))},
                    })
                except Exception:
                    pass
        return items

    def get_all(self, type_filter: str = "", search: str = "") -> List[Dict]:
        """Obtener todos los items con filtros y búsqueda en contenido"""
        items = []
        items.extend(self.scan_chats())
        items.extend(self.scan_documents())
        items.extend(self.scan_gallery())
        items.extend(self.scan_notes())
        items.extend(self.scan_research())

        if type_filter:
            items = [i for i in items if i["type"] == type_filter]

        if search:
            search_lower = search.lower()
            words = [w for w in search_lower.split() if len(w) > 2]
            matched = []
            for i in items:
                score = 0
                title = i.get("title", "").lower()
                tags = [t.lower() for t in i.get("tags", [])]
                # Title match
                if search_lower in title:
                    score += 5
                elif any(w in title for w in words):
                    score += 3
                # Tag match
                if any(search_lower in t for t in tags):
                    score += 4
                elif any(any(w in t for w in words) for t in tags):
                    score += 2
                # Content match (for notes and research)
                if i["type"] in ("note", "research", "document"):
                    path = i.get("path")
                    if path:
                        try:
                            content = Path(path).read_text(encoding="utf-8", errors="ignore")[:5000].lower()
                            if search_lower in content:
                                score += 3
                            elif any(w in content for w in words):
                                score += 1
                        except Exception:
                            pass
                if score > 0:
                    matched.append((score, i))
            matched.sort(key=lambda x: x[0], reverse=True)
            items = [i for _, i in matched]

        items.sort(key=lambda i: i.get("created_at", 0), reverse=True)
        return items

    def get_stats(self) -> Dict:
        all_items = self.get_all()
        by_type = {}
        for item in all_items:
            t = item["type"]
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total": len(all_items),
            "by_type": by_type,
        }
