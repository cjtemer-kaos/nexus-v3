"""
Auto-Organize Chats - SuperNEXUS v2
Asignacion AI-powered de chats a carpetas.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("NEXUS_DATA", Path.home() / ".nexus")) / "chats"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FOLDERS_FILE = DATA_DIR / "folders.json"

THROWAWAY_NAMES = {
    "test", "asdf", "hello", "hi", "yo", "prueba", "hola", "hey",
    "1", "a", "b", "temp", "tmp", "nuevo", "new", "chat",
}

MIN_MESSAGES = 2
MIN_CONTENT_LENGTH = 20


class ChatOrganizer:
    """Organiza chats en carpetas usando AI."""

    def __init__(self, llm_caller: Optional[Callable] = None):
        self.folders: Dict[str, List[str]] = {}
        self._llm_caller = llm_caller
        self._load()

    def _load(self):
        try:
            if FOLDERS_FILE.exists():
                data = json.loads(FOLDERS_FILE.read_text(encoding="utf-8"))
                self.folders = data.get("folders", {})
        except Exception as e:
            logger.error(f"Error cargando carpetas: {e}")

    def _save(self):
        try:
            FOLDERS_FILE.write_text(json.dumps({
                "folders": self.folders
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error guardando carpetas: {e}")

    def _is_throwaway(self, session: Dict) -> bool:
        """Determinar si una sesion es trivial"""
        name = (session.get("name") or "").lower().strip()
        if name in THROWAWAY_NAMES:
            return True
        messages = session.get("messages", [])
        if len(messages) < MIN_MESSAGES:
            return True
        total_chars = sum(len(m.get("content", "")) for m in messages)
        if total_chars < MIN_CONTENT_LENGTH:
            return True
        return False

    def _is_incognito(self, session: Dict) -> bool:
        name = (session.get("name") or "").lower().strip()
        return "incognito" in name or session.get("incognito", False)

    async def auto_sort(self, sessions: List[Dict], delete_throwaway: bool = True) -> Dict:
        """Auto-organizar sesiones en carpetas"""
        deleted_empty = 0
        deleted_throwaway = 0

        if delete_throwaway:
            to_delete = []
            for s in sessions:
                if self._is_incognito(s) or self._is_throwaway(s):
                    to_delete.append(s.get("id"))
                    deleted_throwaway += 1
            sessions = [s for s in sessions if s.get("id") not in to_delete]

        unfiled = [s for s in sessions if not self._get_folder(s.get("id"))]

        if not unfiled:
            return {"updated": 0, "folders": {}, "deleted_throwaway": deleted_throwaway}

        if self._llm_caller and len(unfiled) > 0:
            try:
                batch = unfiled[:15]
                session_list = []
                for s in batch:
                    msgs = s.get("messages", [])
                    preview = msgs[0].get("content", "")[:100] if msgs else ""
                    session_list.append({
                        "id": s.get("id"),
                        "name": s.get("name", "Sin nombre"),
                        "preview": preview,
                        "message_count": len(msgs),
                    })

                prompt = (
                    "Organiza estas sesiones de chat en carpetas logicas. "
                    "Retorna JSON: {\"folders\": {\"Nombre Carpeta\": [\"id1\", \"id2\"]}}\n"
                    f"Sesiones:\n{json.dumps(session_list, ensure_ascii=False)}"
                )
                response = await self._llm_caller(prompt, "director")
                match = re.search(r'\{[^{}]*"folders"[^{}]*\{[^}]+\}[^}]*\}', response)
                if match:
                    parsed = json.loads(match.group())
                    for folder_name, session_ids in parsed.get("folders", {}).items():
                        if folder_name not in self.folders:
                            self.folders[folder_name] = []
                        for sid in session_ids:
                            if sid not in self.folders[folder_name]:
                                self.folders[folder_name].append(sid)
                    self._save()
                    return {
                        "updated": len(batch),
                        "folders": parsed.get("folders", {}),
                        "deleted_throwaway": deleted_throwaway,
                    }
            except Exception as e:
                logger.error(f"AI folder assignment error: {e}")

        return {"updated": 0, "folders": {}, "deleted_throwaway": deleted_throwaway}

    def _get_folder(self, session_id: str) -> Optional[str]:
        for folder, ids in self.folders.items():
            if session_id in ids:
                return folder
        return None

    def move_to_folder(self, session_id: str, folder: str):
        for f, ids in self.folders.items():
            if session_id in ids:
                ids.remove(session_id)
        if folder not in self.folders:
            self.folders[folder] = []
        if session_id not in self.folders[folder]:
            self.folders[folder].append(session_id)
        self._save()

    def create_folder(self, name: str) -> bool:
        if name not in self.folders:
            self.folders[name] = []
            self._save()
            return True
        return False

    def delete_folder(self, name: str) -> bool:
        if name in self.folders:
            del self.folders[name]
            self._save()
            return True
        return False

    def list_folders(self) -> Dict[str, List[str]]:
        return dict(self.folders)

    def get_stats(self) -> Dict:
        total_sessions = sum(len(ids) for ids in self.folders.values())
        return {
            "folders": len(self.folders),
            "organized_sessions": total_sessions,
        }
