"""
TimeTracker - Time tracking y reportes de productividad para SuperNEXUS v2.0

Características:
- Tracking de tiempo por tarea y proyecto
- Estimación de duración basada en tareas similares
- Reportes de productividad
- Análisis de patrones de trabajo
"""

import logging
import json
import hashlib
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class TimeEntry:
    """Entrada de tiempo"""
    id: str
    task: str
    project: str
    gem: str
    engine: str
    started_at: str
    completed_at: str = ""
    duration_seconds: float = 0.0
    success: bool = False
    tags: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        if not self.id:
            self.id = hashlib.md5(f"{self.task}{self.started_at}".encode()).hexdigest()[:12]


class TimeTracker:
    """
    Time tracking y reportes de productividad.
    """
    
    def __init__(self, storage_path: str = None):
        self.entries: List[TimeEntry] = []
        self.active_entries: Dict[str, TimeEntry] = {}
        self.storage_path = Path(storage_path) if storage_path else None
        
        if self.storage_path and self.storage_path.exists():
            self.load()
    
    def start_task(self, task: str, project: str = "default", gem: str = "auto", engine: str = "nexus_master", tags: List[str] = None) -> str:
        """Inicia tracking de tarea"""
        entry = TimeEntry(
            id="",
            task=task,
            project=project,
            gem=gem,
            engine=engine,
            started_at=datetime.now().isoformat(),
            tags=tags or [],
        )
        
        self.active_entries[entry.id] = entry
        logger.info(f"Task started: {entry.id} ({task[:50]})")
        
        return entry.id
    
    def complete_task(self, entry_id: str, success: bool = True):
        """Completa tracking de tarea"""
        if entry_id not in self.active_entries:
            logger.error(f"Active entry not found: {entry_id}")
            return
        
        entry = self.active_entries[entry_id]
        entry.completed_at = datetime.now().isoformat()
        entry.success = success
        
        started = datetime.fromisoformat(entry.started_at)
        completed = datetime.fromisoformat(entry.completed_at)
        entry.duration_seconds = (completed - started).total_seconds()
        
        self.entries.append(entry)
        del self.active_entries[entry_id]
        
        logger.info(f"Task completed: {entry_id} ({entry.duration_seconds:.1f}s)")
        
        self._save()
    
    def get_productivity_report(
        self,
        days: int = 7,
        project: str = None,
        gem: str = None,
    ) -> Dict:
        """Genera reporte de productividad"""
        cutoff = datetime.now() - timedelta(days=days)
        
        filtered = [
            e for e in self.entries
            if datetime.fromisoformat(e.completed_at) > cutoff
        ]
        
        if project:
            filtered = [e for e in filtered if e.project == project]
        if gem:
            filtered = [e for e in filtered if e.gem == gem]
        
        total_tasks = len(filtered)
        successful_tasks = sum(1 for e in filtered if e.success)
        total_time = sum(e.duration_seconds for e in filtered)
        
        avg_duration = total_time / total_tasks if total_tasks > 0 else 0
        
        by_gem = defaultdict(lambda: {"count": 0, "total_time": 0, "success": 0})
        for entry in filtered:
            by_gem[entry.gem]["count"] += 1
            by_gem[entry.gem]["total_time"] += entry.duration_seconds
            if entry.success:
                by_gem[entry.gem]["success"] += 1
        
        by_project = defaultdict(lambda: {"count": 0, "total_time": 0})
        for entry in filtered:
            by_project[entry.project]["count"] += 1
            by_project[entry.project]["total_time"] += entry.duration_seconds
        
        hourly_distribution = defaultdict(int)
        for entry in filtered:
            hour = datetime.fromisoformat(entry.started_at).hour
            hourly_distribution[hour] += 1
        
        return {
            "period_days": days,
            "total_tasks": total_tasks,
            "successful_tasks": successful_tasks,
            "success_rate": successful_tasks / total_tasks if total_tasks > 0 else 0,
            "total_time_seconds": total_time,
            "total_time_hours": total_time / 3600,
            "avg_duration_seconds": avg_duration,
            "by_gem": dict(by_gem),
            "by_project": dict(by_project),
            "hourly_distribution": dict(hourly_distribution),
        }
    
    def estimate_task_duration(self, task_type: str) -> Optional[float]:
        """Estima duración de tarea basada en historial"""
        similar = [
            e for e in self.entries
            if task_type.lower() in e.task.lower()
        ]
        
        if not similar:
            return None
        
        durations = [e.duration_seconds for e in similar if e.duration_seconds > 0]
        
        if not durations:
            return None
        
        return sum(durations) / len(durations)
    
    def get_active_tasks(self) -> List[Dict]:
        """Obtiene tareas activas"""
        return [
            {
                "id": entry.id,
                "task": entry.task,
                "project": entry.project,
                "gem": entry.gem,
                "engine": entry.engine,
                "started_at": entry.started_at,
                "running_seconds": (
                    datetime.now() - datetime.fromisoformat(entry.started_at)
                ).total_seconds(),
            }
            for entry in self.active_entries.values()
        ]
    
    def get_status(self) -> Dict:
        """Obtiene estado de time tracking"""
        return {
            "total_entries": len(self.entries),
            "active_entries": len(self.active_entries),
            "active_tasks": self.get_active_tasks(),
        }
    
    def _save(self):
        """Guarda en disco"""
        if not self.storage_path:
            return
        
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "entries": [
                {
                    "id": e.id,
                    "task": e.task,
                    "project": e.project,
                    "gem": e.gem,
                    "engine": e.engine,
                    "started_at": e.started_at,
                    "completed_at": e.completed_at,
                    "duration_seconds": e.duration_seconds,
                    "success": e.success,
                    "tags": e.tags,
                }
                for e in self.entries
            ],
        }
        
        atomic_write_json(self.storage_path, data)
    
    def load(self):
        """Carga desde disco"""
        if not self.storage_path or not self.storage_path.exists():
            return
        
        with open(self.storage_path, "r") as f:
            data = json.load(f)
        
        self.entries = [
            TimeEntry(
                id=e["id"],
                task=e["task"],
                project=e["project"],
                gem=e["gem"],
                engine=e["engine"],
                started_at=e["started_at"],
                completed_at=e.get("completed_at", ""),
                duration_seconds=e.get("duration_seconds", 0.0),
                success=e.get("success", False),
                tags=e.get("tags", []),
            )
            for e in data.get("entries", [])
        ]
        
        logger.info(f"Time tracker loaded: {len(self.entries)} entries")
