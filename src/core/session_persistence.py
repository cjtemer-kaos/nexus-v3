"""
SessionPersistence - Persistencia de contexto entre sesiones para SuperNEXUS v2.0

Solución para que Nexus NO olvide su contexto entre sesiones.
Guarda estado, decisiones, tareas pendientes y memoria de trabajo en disco.
"""

import json
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """Estado completo de la sesión"""
    session_id: str
    started_at: str
    last_active: str
    current_project: str
    active_tasks: List[Dict] = field(default_factory=list)
    pending_decisions: List[Dict] = field(default_factory=list)
    recent_actions: List[Dict] = field(default_factory=list)
    context_notes: List[str] = field(default_factory=list)
    user_preferences: Dict = field(default_factory=dict)
    system_state: Dict = field(default_factory=dict)
    memory_snapshot: Dict = field(default_factory=dict)


class SessionPersistence:
    """
    Persistencia de contexto entre sesiones.
    
    Uso:
        persistence = SessionPersistence(storage_path="data/session_state.json")
        
        # Guardar estado
        persistence.save_state(
            current_project="supernexus-v2",
            active_tasks=[...],
            context_notes=["Implementando mejoras..."]
        )
        
        # Cargar estado en nueva sesión
        state = persistence.load_state()
    """
    
    def __init__(self, storage_path: str = None):
        self.storage_path = Path(storage_path) if storage_path else None
        self.current_state: Optional[SessionState] = None
    
    def create_session(self, session_id: str = None) -> SessionState:
        """Crea nueva sesión"""
        import hashlib
        if not session_id:
            session_id = hashlib.md5(datetime.now().isoformat().encode()).hexdigest()[:12]
        
        state = SessionState(
            session_id=session_id,
            started_at=datetime.now().isoformat(),
            last_active=datetime.now().isoformat(),
            current_project="default",
        )
        
        self.current_state = state
        self._save()
        
        logger.info(f"Session created: {session_id}")
        return state
    
    def save_state(
        self,
        current_project: str = None,
        active_tasks: List[Dict] = None,
        pending_decisions: List[Dict] = None,
        recent_actions: List[Dict] = None,
        context_notes: List[str] = None,
        user_preferences: Dict = None,
        system_state: Dict = None,
        memory_snapshot: Dict = None,
    ):
        """Guarda estado actual"""
        if not self.current_state:
            self.create_session()
        
        if current_project is not None:
            self.current_state.current_project = current_project
        if active_tasks is not None:
            self.current_state.active_tasks = active_tasks
        if pending_decisions is not None:
            self.current_state.pending_decisions = pending_decisions
        if recent_actions is not None:
            self.current_state.recent_actions = recent_actions
        if context_notes is not None:
            self.current_state.context_notes = context_notes
        if user_preferences is not None:
            self.current_state.user_preferences = user_preferences
        if system_state is not None:
            self.current_state.system_state = system_state
        if memory_snapshot is not None:
            self.current_state.memory_snapshot = memory_snapshot
        
        self.current_state.last_active = datetime.now().isoformat()
        self._save()
    
    def add_context_note(self, note: str):
        """Agrega nota de contexto"""
        if not self.current_state:
            self.create_session()
        
        self.current_state.context_notes.append(note)
        self.current_state.last_active = datetime.now().isoformat()
        self._save()
    
    def add_active_task(self, task: Dict):
        """Agrega tarea activa"""
        if not self.current_state:
            self.create_session()
        
        self.current_state.active_tasks.append(task)
        self.current_state.last_active = datetime.now().isoformat()
        self._save()
    
    def complete_task(self, task_id: str):
        """Marca tarea como completada"""
        if not self.current_state:
            return
        
        self.current_state.active_tasks = [
            t for t in self.current_state.active_tasks
            if t.get("id") != task_id
        ]
        self.current_state.recent_actions.append({
            "action": "task_completed",
            "task_id": task_id,
            "timestamp": datetime.now().isoformat(),
        })
        self._save()
    
    def load_state(self) -> Optional[SessionState]:
        """Carga estado desde disco"""
        if not self.storage_path or not self.storage_path.exists():
            return None
        
        try:
            with open(self.storage_path, "r") as f:
                data = json.load(f)
            
            state = SessionState(
                session_id=data["session_id"],
                started_at=data["started_at"],
                last_active=data["last_active"],
                current_project=data["current_project"],
                active_tasks=data.get("active_tasks", []),
                pending_decisions=data.get("pending_decisions", []),
                recent_actions=data.get("recent_actions", []),
                context_notes=data.get("context_notes", []),
                user_preferences=data.get("user_preferences", {}),
                system_state=data.get("system_state", {}),
                memory_snapshot=data.get("memory_snapshot", {}),
            )
            
            self.current_state = state
            logger.info(f"Session loaded: {state.session_id} (last active: {state.last_active})")
            
            return state
        except Exception as e:
            logger.error(f"Failed to load session: {e}")
            return None
    
    def get_context_summary(self) -> str:
        """Obtiene resumen de contexto para inyectar en prompt"""
        if not self.current_state:
            return "No previous session context."
        
        summary = f"""## Contexto de Sesión Anterior

**Sesión ID:** {self.current_state.session_id}
**Proyecto Activo:** {self.current_state.current_project}
**Última Actividad:** {self.current_state.last_active}

### Tareas Activas ({len(self.current_state.active_tasks)})
"""
        
        for task in self.current_state.active_tasks:
            summary += f"- {task.get('title', 'Unknown')}: {task.get('status', 'pending')}\n"
        
        if self.current_state.context_notes:
            summary += f"\n### Notas de Contexto ({len(self.current_state.context_notes)})\n"
            for note in self.current_state.context_notes[-10:]:
                summary += f"- {note}\n"
        
        if self.current_state.pending_decisions:
            summary += f"\n### Decisiones Pendientes ({len(self.current_state.pending_decisions)})\n"
            for decision in self.current_state.pending_decisions:
                summary += f"- {decision.get('title', 'Unknown')}\n"
        
        return summary
    
    def _save(self):
        """Guarda en disco"""
        if not self.storage_path:
            return
        
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "session_id": self.current_state.session_id,
            "started_at": self.current_state.started_at,
            "last_active": self.current_state.last_active,
            "current_project": self.current_state.current_project,
            "active_tasks": self.current_state.active_tasks,
            "pending_decisions": self.current_state.pending_decisions,
            "recent_actions": self.current_state.recent_actions,
            "context_notes": self.current_state.context_notes,
            "user_preferences": self.current_state.user_preferences,
            "system_state": self.current_state.system_state,
            "memory_snapshot": self.current_state.memory_snapshot,
        }
        
        atomic_write_json(self.storage_path, data)
    
    def get_status(self) -> Dict:
        return {
            "has_session": self.current_state is not None,
            "session_id": self.current_state.session_id if self.current_state else None,
            "current_project": self.current_state.current_project if self.current_state else None,
            "active_tasks": len(self.current_state.active_tasks) if self.current_state else 0,
            "context_notes": len(self.current_state.context_notes) if self.current_state else 0,
            "last_active": self.current_state.last_active if self.current_state else None,
        }
