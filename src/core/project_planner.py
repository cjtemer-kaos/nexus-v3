"""
ProjectPlanner - Planificador de proyectos con hitos para SuperNEXUS v2.0

Características:
- Creación de proyectos con hitos y deadlines
- Seguimiento automático de progreso
- Alertas de hitos próximos
- Estimación de tiempo basada en tareas similares
"""

import logging
import json
import hashlib
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from enum import Enum

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


class MilestoneStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ProjectStatus(Enum):
    PLANNING = "planning"
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class Milestone:
    """Hito de proyecto"""
    id: str
    title: str
    description: str
    status: MilestoneStatus = MilestoneStatus.PENDING
    created_at: str = ""
    due_date: str = ""
    completed_at: str = ""
    progress: float = 0.0
    tags: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.id:
            self.id = hashlib.md5(f"{self.title}{self.created_at}".encode()).hexdigest()[:12]


@dataclass
class Project:
    """Proyecto con hitos"""
    id: str
    name: str
    description: str
    status: ProjectStatus = ProjectStatus.PLANNING
    created_at: str = ""
    updated_at: str = ""
    milestones: List[Milestone] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = datetime.now().isoformat()
        if not self.id:
            self.id = hashlib.md5(f"{self.name}{self.created_at}".encode()).hexdigest()[:12]


class ProjectPlanner:
    """
    Planificador de proyectos con hitos para SuperNEXUS v2.0
    
    Uso:
        planner = ProjectPlanner()
        project = planner.create_project("Mejorar UI", "Mejorar la interfaz de usuario")
        planner.add_milestone(project.id, "Diseñar mockups", due_date="2026-05-20")
        planner.update_milestone_progress(project.id, milestone_id, 50.0)
    """
    
    def __init__(self, storage_path: str = None):
        self.projects: Dict[str, Project] = {}
        self.storage_path = Path(storage_path) if storage_path else None
        
        if self.storage_path and self.storage_path.exists():
            self.load()
    
    def create_project(
        self,
        name: str,
        description: str,
        tags: List[str] = None,
        metadata: Dict = None,
    ) -> Project:
        """Crea nuevo proyecto"""
        project = Project(
            id="",
            name=name,
            description=description,
            tags=tags or [],
            metadata=metadata or {},
        )
        
        self.projects[project.id] = project
        logger.info(f"Project created: {project.id} ({name})")
        
        self._save()
        return project
    
    def add_milestone(
        self,
        project_id: str,
        title: str,
        description: str = "",
        due_date: str = "",
        tags: List[str] = None,
        dependencies: List[str] = None,
    ) -> Optional[Milestone]:
        """Agrega hito a proyecto"""
        if project_id not in self.projects:
            logger.error(f"Project not found: {project_id}")
            return None
        
        milestone = Milestone(
            id="",
            title=title,
            description=description,
            due_date=due_date,
            tags=tags or [],
            dependencies=dependencies or [],
        )
        
        self.projects[project_id].milestones.append(milestone)
        self.projects[project_id].updated_at = datetime.now().isoformat()
        
        logger.info(f"Milestone added: {milestone.id} ({title})")
        
        self._save()
        return milestone
    
    def update_milestone_status(
        self,
        project_id: str,
        milestone_id: str,
        status: MilestoneStatus,
    ) -> bool:
        """Actualiza estado de hito"""
        project = self.projects.get(project_id)
        if not project:
            return False
        
        for milestone in project.milestones:
            if milestone.id == milestone_id:
                milestone.status = status
                
                if status == MilestoneStatus.COMPLETED:
                    milestone.completed_at = datetime.now().isoformat()
                    milestone.progress = 100.0
                
                project.updated_at = datetime.now().isoformat()
                
                self._check_project_completion(project)
                self._save()
                
                return True
        
        return False
    
    def update_milestone_progress(
        self,
        project_id: str,
        milestone_id: str,
        progress: float,
    ) -> bool:
        """Actualiza progreso de hito"""
        project = self.projects.get(project_id)
        if not project:
            return False
        
        for milestone in project.milestones:
            if milestone.id == milestone_id:
                milestone.progress = max(0.0, min(100.0, progress))
                
                if milestone.progress == 100.0:
                    milestone.status = MilestoneStatus.COMPLETED
                    milestone.completed_at = datetime.now().isoformat()
                elif milestone.progress > 0:
                    milestone.status = MilestoneStatus.IN_PROGRESS
                
                project.updated_at = datetime.now().isoformat()
                
                self._check_project_completion(project)
                self._save()
                
                return True
        
        return False
    
    def _check_project_completion(self, project: Project):
        """Verifica si proyecto está completo"""
        if not project.milestones:
            return
        
        completed = sum(
            1 for m in project.milestones
            if m.status == MilestoneStatus.COMPLETED
        )
        
        if completed == len(project.milestones):
            project.status = ProjectStatus.COMPLETED
            logger.info(f"Project completed: {project.name}")
    
    def get_upcoming_milestones(self, days: int = 7) -> List[Dict]:
        """Obtiene hitos próximos"""
        now = datetime.now()
        upcoming = []
        
        for project in self.projects.values():
            for milestone in project.milestones:
                if milestone.due_date and milestone.status != MilestoneStatus.COMPLETED:
                    due = datetime.fromisoformat(milestone.due_date)
                    if (due - now).days <= days:
                        upcoming.append({
                            "project_id": project.id,
                            "project_name": project.name,
                            "milestone_id": milestone.id,
                            "title": milestone.title,
                            "due_date": milestone.due_date,
                            "days_remaining": (due - now).days,
                            "status": milestone.status.value,
                        })
        
        upcoming.sort(key=lambda x: x["days_remaining"])
        return upcoming
    
    def get_project_progress(self, project_id: str) -> float:
        """Obtiene progreso de proyecto"""
        project = self.projects.get(project_id)
        if not project or not project.milestones:
            return 0.0
        
        total_progress = sum(m.progress for m in project.milestones)
        return total_progress / len(project.milestones)
    
    def get_project_status(self, project_id: str) -> Dict:
        """Obtiene estado completo de proyecto"""
        project = self.projects.get(project_id)
        if not project:
            return {"error": "Project not found"}
        
        milestones_status = {}
        for m in project.milestones:
            milestones_status[m.id] = {
                "title": m.title,
                "status": m.status.value,
                "progress": m.progress,
                "due_date": m.due_date,
                "completed_at": m.completed_at,
            }
        
        return {
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "status": project.status.value,
            "progress": self.get_project_progress(project_id),
            "milestones": milestones_status,
            "total_milestones": len(project.milestones),
            "completed_milestones": sum(
                1 for m in project.milestones
                if m.status == MilestoneStatus.COMPLETED
            ),
            "created_at": project.created_at,
            "updated_at": project.updated_at,
        }
    
    def delete_project(self, project_id: str) -> bool:
        """Elimina proyecto"""
        if project_id in self.projects:
            del self.projects[project_id]
            self._save()
            return True
        return False
    
    def list_projects(self, status: ProjectStatus = None) -> List[Dict]:
        """Lista proyectos"""
        projects = []
        
        for project in self.projects.values():
            if status and project.status != status:
                continue
            
            projects.append({
                "id": project.id,
                "name": project.name,
                "description": project.description,
                "status": project.status.value,
                "progress": self.get_project_progress(project.id),
                "milestones": len(project.milestones),
                "created_at": project.created_at,
            })
        
        return projects
    
    def _save(self):
        """Guarda en disco"""
        if not self.storage_path:
            return
        
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "projects": {
                project_id: {
                    "id": project.id,
                    "name": project.name,
                    "description": project.description,
                    "status": project.status.value,
                    "created_at": project.created_at,
                    "updated_at": project.updated_at,
                    "tags": project.tags,
                    "metadata": project.metadata,
                    "milestones": [
                        {
                            "id": m.id,
                            "title": m.title,
                            "description": m.description,
                            "status": m.status.value,
                            "created_at": m.created_at,
                            "due_date": m.due_date,
                            "completed_at": m.completed_at,
                            "progress": m.progress,
                            "tags": m.tags,
                            "dependencies": m.dependencies,
                        }
                        for m in project.milestones
                    ],
                }
                for project_id, project in self.projects.items()
            }
        }
        
        atomic_write_json(self.storage_path, data)
    
    def load(self):
        """Carga desde disco"""
        if not self.storage_path or not self.storage_path.exists():
            return
        
        with open(self.storage_path, "r") as f:
            data = json.load(f)
        
        self.projects.clear()
        
        for project_id, project_data in data.get("projects", {}).items():
            milestones = [
                Milestone(
                    id=m["id"],
                    title=m["title"],
                    description=m["description"],
                    status=MilestoneStatus(m["status"]),
                    created_at=m["created_at"],
                    due_date=m.get("due_date", ""),
                    completed_at=m.get("completed_at", ""),
                    progress=m.get("progress", 0.0),
                    tags=m.get("tags", []),
                    dependencies=m.get("dependencies", []),
                )
                for m in project_data.get("milestones", [])
            ]
            
            project = Project(
                id=project_data["id"],
                name=project_data["name"],
                description=project_data["description"],
                status=ProjectStatus(project_data["status"]),
                created_at=project_data["created_at"],
                updated_at=project_data["updated_at"],
                tags=project_data.get("tags", []),
                metadata=project_data.get("metadata", {}),
                milestones=milestones,
            )
            
            self.projects[project_id] = project
        
        logger.info(f"Project planner loaded: {len(self.projects)} projects")
