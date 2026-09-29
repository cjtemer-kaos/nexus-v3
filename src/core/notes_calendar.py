"""
Notes + Calendar - SuperNEXUS v2
Notas con recordatorios, checklist, repeat, calendar con CalDAV sync.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("NEXUS_DATA", Path.home() / ".nexus")) / "notes"
DATA_DIR.mkdir(parents=True, exist_ok=True)
NOTES_FILE = DATA_DIR / "notes.json"
CALENDAR_FILE = DATA_DIR / "calendar.json"


class Note:
    def __init__(self, data: Dict):
        self.id: str = data.get("id", str(uuid.uuid4())[:8])
        self.title: str = data.get("title", "")
        self.content: str = data.get("content", "")
        self.items: List[Dict] = data.get("items", [])
        self.note_type: str = data.get("note_type", "note")
        self.color: str = data.get("color", "#00d4ff")
        self.label: str = data.get("label", "")
        self.pinned: bool = data.get("pinned", False)
        self.archived: bool = data.get("archived", False)
        self.due_date: Optional[str] = data.get("due_date")
        self.repeat: str = data.get("repeat", "none")
        self.source: str = data.get("source", "user")
        self.created_at: float = data.get("created_at", time.time())
        self.updated_at: float = data.get("updated_at", time.time())
        self.reminder_sent: bool = data.get("reminder_sent", False)

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "title": self.title, "content": self.content,
            "items": self.items, "note_type": self.note_type, "color": self.color,
            "label": self.label, "pinned": self.pinned, "archived": self.archived,
            "due_date": self.due_date, "repeat": self.repeat, "source": self.source,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "reminder_sent": self.reminder_sent,
        }


class CalendarEvent:
    def __init__(self, data: Dict):
        self.uid: str = data.get("uid", str(uuid.uuid4())[:8])
        self.summary: str = data.get("summary", "")
        self.description: str = data.get("description", "")
        self.start: str = data.get("start", "")
        self.end: str = data.get("end", "")
        self.all_day: bool = data.get("all_day", False)
        self.calendar: str = data.get("calendar", "default")
        self.event_type: str = data.get("event_type", "other")
        self.importance: str = data.get("importance", "normal")
        self.recurrence: str = data.get("recurrence", "")
        self.location: str = data.get("location", "")
        self.created_at: float = data.get("created_at", time.time())

    def to_dict(self) -> Dict:
        return {
            "uid": self.uid, "summary": self.summary, "description": self.description,
            "start": self.start, "end": self.end, "all_day": self.all_day,
            "calendar": self.calendar, "event_type": self.event_type,
            "importance": self.importance, "recurrence": self.recurrence,
            "location": self.location, "created_at": self.created_at,
        }


class NotesManager:
    """Gestor de notas con recordatorios y repeat."""

    def __init__(self):
        self.notes: Dict[str, Note] = {}
        self._load()

    def _load(self):
        try:
            if NOTES_FILE.exists():
                data = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
                for n in data.get("notes", []):
                    note = Note(n)
                    self.notes[note.id] = note
        except Exception as e:
            logger.error(f"Error cargando notas: {e}")

    def _save(self):
        try:
            NOTES_FILE.write_text(json.dumps({
                "notes": [n.to_dict() for n in self.notes.values()]
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error guardando notas: {e}")

    def create(self, data: Dict) -> Note:
        note = Note({"created_at": time.time(), "updated_at": time.time(), **data})
        self.notes[note.id] = note
        self._save()
        return note

    def update(self, note_id: str, updates: Dict) -> Optional[Note]:
        note = self.notes.get(note_id)
        if not note:
            return None
        for k, v in updates.items():
            if hasattr(note, k):
                setattr(note, k, v)
        note.updated_at = time.time()
        self._save()
        return note

    def delete(self, note_id: str) -> bool:
        if note_id in self.notes:
            del self.notes[note_id]
            self._save()
            return True
        return False

    def list_notes(self, label: str = "", archived: bool = False, pinned: bool = False) -> List[Dict]:
        notes = list(self.notes.values())
        if label:
            notes = [n for n in notes if n.label == label]
        if not archived:
            notes = [n for n in notes if not n.archived]
        if pinned:
            notes = [n for n in notes if n.pinned]
        notes.sort(key=lambda n: (not n.pinned, -n.updated_at))
        return [n.to_dict() for n in notes]

    def toggle_pin(self, note_id: str) -> bool:
        note = self.notes.get(note_id)
        if note:
            note.pinned = not note.pinned
            self._save()
            return note.pinned
        return False

    def toggle_archive(self, note_id: str) -> bool:
        note = self.notes.get(note_id)
        if note:
            note.archived = not note.archived
            self._save()
            return note.archived
        return False

    def toggle_item(self, note_id: str, item_index: int) -> bool:
        note = self.notes.get(note_id)
        if note and 0 <= item_index < len(note.items):
            note.items[item_index]["done"] = not note.items[item_index].get("done", False)
            self._save()
            return True
        return False

    def get_due_notes(self) -> List[Note]:
        """Notas con due_date que vencen pronto"""
        now = datetime.now()
        due = []
        for note in self.notes.values():
            if note.due_date and not note.reminder_sent and not note.archived:
                try:
                    due_dt = datetime.fromisoformat(note.due_date)
                    if due_dt <= now + timedelta(minutes=30):
                        due.append(note)
                except Exception:
                    pass
        return due

    def mark_reminder_sent(self, note_id: str):
        note = self.notes.get(note_id)
        if note:
            note.reminder_sent = True
            self._save()

    def get_stats(self) -> Dict:
        active = [n for n in self.notes.values() if not n.archived]
        pinned = [n for n in active if n.pinned]
        with_due = [n for n in active if n.due_date]
        return {
            "total": len(self.notes), "active": len(active),
            "pinned": len(pinned), "archived": len(self.notes) - len(active),
            "with_due": len(with_due),
        }


class CalendarManager:
    """Gestor de calendario con eventos y recurrence."""

    def __init__(self):
        self.events: Dict[str, CalendarEvent] = {}
        self._load()

    def _load(self):
        try:
            if CALENDAR_FILE.exists():
                data = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
                for e in data.get("events", []):
                    event = CalendarEvent(e)
                    self.events[event.uid] = event
        except Exception as e:
            logger.error(f"Error cargando calendario: {e}")

    def _save(self):
        try:
            CALENDAR_FILE.write_text(json.dumps({
                "events": [e.to_dict() for e in self.events.values()]
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error guardando calendario: {e}")

    def create_event(self, data: Dict) -> CalendarEvent:
        event = CalendarEvent({"created_at": time.time(), **data})
        self.events[event.uid] = event
        self._save()
        return event

    def update_event(self, uid: str, updates: Dict) -> Optional[CalendarEvent]:
        event = self.events.get(uid)
        if not event:
            return None
        for k, v in updates.items():
            if hasattr(event, k):
                setattr(event, k, v)
        self._save()
        return event

    def delete_event(self, uid: str) -> bool:
        if uid in self.events:
            del self.events[uid]
            self._save()
            return True
        return False

    def list_events(self, start: str = "", end: str = "") -> List[Dict]:
        events = list(self.events.values())
        if start:
            events = [e for e in events if e.start >= start]
        if end:
            events = [e for e in events if e.start <= end]
        events.sort(key=lambda e: e.start)
        return [e.to_dict() for e in events]

    def get_upcoming(self, days: int = 7) -> List[Dict]:
        now = datetime.now()
        end = now + timedelta(days=days)
        return self.list_events(
            start=now.isoformat(),
            end=end.isoformat(),
        )

    def get_stats(self) -> Dict:
        return {"total_events": len(self.events)}
