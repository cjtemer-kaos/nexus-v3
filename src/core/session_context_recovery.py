"""
SessionContextRecovery - Recuperación automática de contexto

Problema: DirectorNexus pierde contexto entre reinicios (memoria a largo plazo no se carga)
y también pierde contexto reciente (últimos 30min-2h) porque SessionManager es volátil.

Solución: 
1. Persistir estado de sesión en disco (JSON) cada N mensajes o al finalizar sesión
2. Al iniciar, cargar automáticamente:
   - Últimas conversaciones recientes (de session_manager)
   - Tareas activas (de shared memory)
   - Observaciones recientes (de nexus_memory.db)
   - Estado del proyecto activo
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """Estado persistente de una sesión"""
    session_id: str
    project: str
    started_at: str
    last_activity: str
    message_count: int = 0
    total_tokens: int = 0
    recent_messages: List[Dict] = field(default_factory=list)  # Últimos 50 mensajes
    active_tasks: List[Dict] = field(default_factory=list)
    context_summary: str = ""


class SessionContextRecovery:
    """
    Sistema de recuperación de contexto para DirectorNexus.
    
    Carga automáticamente el contexto relevante al iniciar sesión:
    - Sesiones recientes persistidas en disco
    - Tareas activas en memoria compartida
    - Observaciones recientes de nexus_memory.db
    - Estado del proyecto activo
    """
    
    def __init__(self, nexus_home: Path = None):
        self.nexus_home = nexus_home or Path.home() / ".nexus"
        self.sessions_dir = self.nexus_home / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        
        # Rutas de persistencia
        self.current_session_file = self.sessions_dir / "current_session.json"
        self.recent_sessions_file = self.sessions_dir / "recent_sessions.json"
        
        # Rutas de memoria
        self.memory_db = self.nexus_home / "brain" / "nexus_memory.db"
        self.message_board_db = Path.home() / ".nexus" / "brain" / "message_board.db"
        
        # Configuración
        self.max_recent_messages = 50
        self.max_recent_sessions = 5
        self.recovery_window_hours = 2  # Ventana de recuperación
        
        logger.info(f"SessionContextRecovery initialized (nexus_home: {self.nexus_home})")
    
    def save_session_state(self, state: SessionState) -> bool:
        """Persistir estado de sesión actual en disco"""
        try:
            # Guardar sesión actual
            atomic_write_json(self.current_session_file, asdict(state))
            
            # Actualizar lista de sesiones recientes
            recent = self._load_recent_sessions()
            recent.append(asdict(state))
            recent = recent[-self.max_recent_sessions:]  # Mantener solo las últimas N
            atomic_write_json(self.recent_sessions_file, recent)
            
            logger.info(f"Session state saved: {state.session_id} ({state.message_count} messages)")
            return True
        except Exception as e:
            logger.error(f"Failed to save session state: {e}")
            return False
    
    def load_current_session(self) -> Optional[SessionState]:
        """Cargar estado de la sesión actual si existe"""
        if not self.current_session_file.exists():
            return None
        
        try:
            with open(self.current_session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return SessionState(**data)
        except Exception as e:
            logger.error(f"Failed to load current session: {e}")
            return None
    
    def load_recent_sessions(self, hours: int = None) -> List[SessionState]:
        """Cargar sesiones recientes dentro de la ventana de tiempo"""
        hours = hours or self.recovery_window_hours
        cutoff = datetime.now() - timedelta(hours=hours)
        
        recent = self._load_recent_sessions()
        filtered = []
        for session_data in recent:
            try:
                last_activity = datetime.fromisoformat(session_data["last_activity"])
                if last_activity >= cutoff:
                    filtered.append(SessionState(**session_data))
            except (ValueError, KeyError):
                continue
        
        return filtered
    
    def _get_shared_memory_conn(self) -> Optional[sqlite3.Connection]:
        """Obtener conexión a shared memory (message_board.db - SOLO LECTURA)"""
        try:
            if self.message_board_db.exists():
                # URI mode con immutable=1 para evitar locks con otros agentes
                uri = f"file:{self.message_board_db}?immutable=1&mode=ro"
                conn = sqlite3.connect(uri, uri=True)
                conn.row_factory = sqlite3.Row
                return conn
        except Exception as e:
            logger.error(f"Failed to connect to shared memory: {e}")
        return None
    
    def load_active_tasks(self) -> List[Dict]:
        """Cargar tareas activas desde shared memory DB"""
        tasks = []
        try:
            conn = self._get_shared_memory_conn()
            if conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM shared_memory WHERE key LIKE 'task:%' OR key LIKE 'agent:%' OR key LIKE 'supernexus%' OR key LIKE 'nexus_%' OR key LIKE 'autopsia%' OR key LIKE 'analisis%' OR key LIKE 'ui-%' OR key LIKE 'cleanup%' OR key LIKE 'optimization%' OR key LIKE 'deepseek%' OR key LIKE 'report:%' OR key LIKE 'agent_workflow%' OR key LIKE 'openclaw%'")
                for row in cursor.fetchall():
                    tasks.append({"key": row["key"], "data": row["value"]})
                conn.close()
        except Exception as e:
            logger.error(f"Failed to load active tasks: {e}")
        
        return tasks
    
    def load_shared_memory_context(self) -> Dict:
        """Cargar contexto desde shared memory DB"""
        context = {}
        try:
            conn = self._get_shared_memory_conn()
            if conn:
                cursor = conn.cursor()
                
                # Claves críticas de contexto
                context_keys = [
                    "supernexus_vision_and_capabilities",
                    "supernexus_v2_context",
                    "supernexus_v2_status",
                    "nexus_protocol_v2",
                    "agent_workflow_rules",
                    "nexus_local_path",
                    "new_repositories_audit_analysis",
                    "supernexus_v2_ideas_and_capabilities",
                    "deepseek_tui_analysis_20260518",
                    "deepseek_findings_antigravity",
                    "autopsia_10_repositorios_antigravity",
                    "analisis_recursos_christian",
                    "antigravity_latest_report",
                    "antigravity_latest_audit_id",
                    "ui-sprint-active",
                    "cleanup_status",
                    "optimization_report_20260518",
                    "openclaw_status",
                ]
                
                for key in context_keys:
                    cursor.execute("SELECT value FROM shared_memory WHERE key = ?", (key,))
                    row = cursor.fetchone()
                    if row:
                        context[key] = row["value"]
                
                conn.close()
        except Exception as e:
            logger.error(f"Failed to load shared memory context: {e}")
        
        return context
    
    def load_recent_observations(self, limit: int = 10, hours: int = None) -> List[Dict]:
        """Cargar observaciones recientes de nexus_memory.db"""
        hours = hours or self.recovery_window_hours
        cutoff = datetime.now() - timedelta(hours=hours)
        
        observations = []
        try:
            if self.memory_db.exists():
                conn = sqlite3.connect(str(self.memory_db))
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Buscar observaciones recientes
                cursor.execute(
                    "SELECT * FROM observations WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                    (cutoff.isoformat(), limit)
                )
                rows = cursor.fetchall()
                for row in rows:
                    observations.append(dict(row))
                conn.close()
        except Exception as e:
            logger.error(f"Failed to load recent observations: {e}")
        
        return observations
    
    def build_session_context(self, project: str = "default") -> Dict[str, Any]:
        """
        Construir contexto completo para recuperación de sesión.
        
        Retorna un diccionario con todo el contexto relevante:
        - Sesión actual (si existe)
        - Sesiones recientes
        - Tareas activas
        - Observaciones recientes
        - Shared memory context (visión, estado, protocolo)
        - Resumen de contexto
        """
        current = self.load_current_session()
        recent = self.load_recent_sessions()
        active_tasks = self.load_active_tasks()
        recent_obs = self.load_recent_observations()
        shared_context = self.load_shared_memory_context()
        
        # Construir resumen de contexto
        context_summary = self._build_context_summary(
            current=current,
            recent=recent,
            active_tasks=active_tasks,
            recent_obs=recent_obs,
            shared_context=shared_context,
        )
        
        return {
            "current_session": asdict(current) if current else None,
            "recent_sessions": [asdict(s) for s in recent],
            "active_tasks": active_tasks,
            "recent_observations": recent_obs,
            "shared_context": shared_context,
            "context_summary": context_summary,
            "recovered_at": datetime.now().isoformat(),
            "project": project,
        }
    
    def _build_context_summary(
        self,
        current: Optional[SessionState],
        recent: List[SessionState],
        active_tasks: List[Dict],
        recent_obs: List[Dict],
        shared_context: Dict,
    ) -> str:
        """Construir resumen legible del contexto recuperado"""
        lines = []
        lines.append("# Contexto Recuperado Automáticamente")
        lines.append("")
        
        # Shared memory context (lo más importante)
        if shared_context.get("vision"):
            lines.append("## Visión y Capacidades de NEXUS IA")
            lines.append(str(shared_context["vision"])[:500])
            lines.append("")
        
        if shared_context.get("protocol"):
            lines.append("## Protocolo NexusHive")
            lines.append(str(shared_context["protocol"])[:300])
            lines.append("")
        
        if shared_context.get("rules"):
            lines.append("## Reglas Críticas para Agentes")
            lines.append(str(shared_context["rules"])[:300])
            lines.append("")
        
        if shared_context.get("status"):
            lines.append("## Estado del Sistema")
            lines.append(str(shared_context["status"])[:300])
            lines.append("")
        
        # Sesión actual
        if current:
            lines.append(f"## Sesión Actual: {current.session_id}")
            lines.append(f"- Proyecto: {current.project}")
            lines.append(f"- Mensajes: {current.message_count}")
            lines.append(f"- Tokens: {current.total_tokens}")
            lines.append(f"- Última actividad: {current.last_activity}")
            lines.append("")
            
            if current.recent_messages:
                lines.append("### Mensajes Recientes")
                for msg in current.recent_messages[-10:]:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")[:200]
                    lines.append(f"- **{role}**: {content}")
                lines.append("")
        
        # Sesiones recientes
        if recent:
            lines.append(f"## Sesiones Recientes ({len(recent)})")
            for session in recent:
                lines.append(f"- {session['session_id']}: {session['message_count']} mensajes ({session['last_activity']})")
            lines.append("")
        
        # Tareas activas
        if active_tasks:
            lines.append(f"## Tareas Activas ({len(active_tasks)})")
            for task in active_tasks[:10]:
                lines.append(f"- `{task['key']}`: {str(task['data'])[:200]}")
            lines.append("")
        
        # Observaciones recientes
        if recent_obs:
            lines.append(f"## Observaciones Recientes ({len(recent_obs)})")
            for obs in recent_obs:
                lines.append(f"- [{obs.get('category', 'general')}] {obs.get('content', '')[:200]}")
            lines.append("")
        
        return "\n".join(lines)
    
    def _load_recent_sessions(self) -> List[Dict]:
        """Cargar lista de sesiones recientes"""
        if not self.recent_sessions_file.exists():
            return []
        
        try:
            with open(self.recent_sessions_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    
    def update_session_activity(self, session_id: str, message: Dict, tokens: int = 0):
        """Actualizar actividad de sesión (llamar después de cada mensaje)"""
        current = self.load_current_session()
        if not current or current.session_id != session_id:
            return
        
        current.last_activity = datetime.now().isoformat()
        current.message_count += 1
        current.total_tokens += tokens
        current.recent_messages.append(message)
        current.recent_messages = current.recent_messages[-self.max_recent_messages:]
        
        self.save_session_state(current)
