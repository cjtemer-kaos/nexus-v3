"""
Incognito Mode - SuperNEXUS v2
Sesiones sin persistencia: mensajes no se guardan en DB, memoria deshabilitada.
"""

import logging
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


class IncognitoManager:
    """Gestiona sesiones incognito — sin persistencia, sin memoria."""

    def __init__(self):
        self._active_sessions: Set[str] = set()

    def is_incognito(self, session_id: Optional[str]) -> bool:
        """Verificar si una sesion esta en modo incognito"""
        return session_id is not None and session_id in self._active_sessions

    def enable(self, session_id: str):
        """Activar incognito para una sesion"""
        self._active_sessions.add(session_id)
        logger.info(f"Incognito activado: {session_id}")

    def disable(self, session_id: str):
        """Desactivar incognito para una sesion"""
        self._active_sessions.discard(session_id)
        logger.info(f"Incognito desactivado: {session_id}")

    def toggle(self, session_id: str) -> bool:
        """Toggle incognito, retorna nuevo estado"""
        if self.is_incognito(session_id):
            self.disable(session_id)
            return False
        else:
            self.enable(session_id)
            return True

    def cleanup_session(self, session_id: str):
        """Limpiar sesion incognito al cerrar"""
        self._active_sessions.discard(session_id)

    def get_blocked_tools(self) -> Set[str]:
        """Herramientas bloqueadas en modo incognito"""
        return {"shell", "terminal", "write_file", "edit_file", "git_commit"}

    def should_persist_message(self, session_id: Optional[str]) -> bool:
        """Determinar si un mensaje debe persistirse"""
        return not self.is_incognito(session_id)

    def should_extract_memory(self, session_id: Optional[str]) -> bool:
        """Determinar si se debe extraer memoria"""
        return not self.is_incognito(session_id)

    def get_status(self) -> Dict:
        """Estado del manager"""
        return {
            "active_sessions": list(self._active_sessions),
            "count": len(self._active_sessions),
        }
