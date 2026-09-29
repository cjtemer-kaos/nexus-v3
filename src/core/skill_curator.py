"""
Skill Curator - Gestor activo del ciclo de vida de habilidades para SuperNEXUS v2

Gestiona habilidades (skills) con estados:
- active → stale → archived

Patrones:
- Deteccion automatica de skills obsoletos por uso
- Archivado de skills no usadas
- Reactivacion de skills archivadas
- Estadisticas de uso por skill
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger("nexus-skill-curator")


@dataclass
class CuratorConfig:
    """Configuracion del Skill Curator"""
    interval_hours: float = 24.0
    stale_after_days: int = 30
    archive_after_days: int = 90
    auto_archive: bool = True


@dataclass
class SkillRecord:
    """Registro de una habilidad"""
    name: str
    version: str = "1.0.0"
    description: str = ""
    category: str = "general"
    state: str = "active"  # active, stale, archived, pinned
    created_at: str = ""
    updated_at: str = ""
    last_used: str = ""
    usage_count: int = 0
    last_error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.state

    def __post_init__(self):
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


class SkillCurator:
    """
    Curador de habilidades con ciclo de vida automatico.

    Uso:
        curator = SkillCurator()
        curator.register_skill("code_review", "1.0.0", "Code review automatico")
        curator.record_usage("code_review")
        curator.review_lifecycle()  # Actualiza estados automaticamente
    """

    def __init__(self, skills_dir: str = None, config: CuratorConfig = None):
        if skills_dir is None:
            skills_dir = str(Path.home() / ".nexus" / "skills")
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._skills: Dict[str, SkillRecord] = {}
        self._config = config or CuratorConfig()
        self._paused = False
        self._run_count = 0
        self._load_skills()

    def _skills_file(self) -> Path:
        return self.skills_dir / "skills_registry.json"

    def _load_skills(self):
        """Carga skills desde disco"""
        skills_file = self._skills_file()
        if skills_file.exists():
            try:
                data = json.loads(skills_file.read_text(encoding="utf-8"))
                for name, record_data in data.items():
                    self._skills[name] = SkillRecord(**record_data)
                logger.info(f"Loaded {len(self._skills)} skills")
            except Exception as e:
                logger.error(f"Error loading skills: {e}")

    def _save_skills(self):
        """Guarda skills en disco"""
        data = {name: {
            "name": s.name,
            "version": s.version,
            "description": s.description,
            "category": s.category,
            "state": s.state,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "last_used": s.last_used,
            "usage_count": s.usage_count,
            "last_error": s.last_error,
            "metadata": s.metadata,
        } for name, s in self._skills.items()}

        atomic_write_json(self._skills_file(), data)

    def register_skill(self, name: str, version: str = "1.0.0", description: str = "", category: str = "general", metadata: Dict = None, tags: List[str] = None) -> SkillRecord:
        """Registra una nueva habilidad"""
        if name in self._skills:
            # Actualizar version si ya existe
            self._skills[name].version = version
            self._skills[name].description = description
            self._skills[name].updated_at = datetime.now().isoformat()
        else:
            self._skills[name] = SkillRecord(
                name=name,
                version=version,
                description=description,
                category=category,
                metadata=metadata or {},
                tags=tags or [],
            )

        self._save_skills()
        logger.info(f"Skill registered: {name} v{version}")
        return self._skills[name]

    def record_usage(self, name: str, success: bool = True, error: str = ""):
        """Registra uso de una habilidad"""
        if name not in self._skills:
            logger.warning(f"Skill not found: {name}")
            return

        skill = self._skills[name]
        skill.usage_count += 1
        skill.last_used = datetime.now().isoformat()
        skill.updated_at = datetime.now().isoformat()

        if success:
            skill.state = "active"  # Reactivar si estaba stale
        else:
            skill.last_error = error

        self._save_skills()

    def review_lifecycle(self) -> Dict[str, List[str]]:
        """
        Revisa y actualiza estados de habilidades.

        Retorna dict con habilidades cambiadas de estado.
        """
        changes = {"stale": [], "archived": [], "reactivated": []}
        now = datetime.now()

        stale_days = self._config.stale_after_days
        archive_days = self._config.archive_after_days

        for name, skill in self._skills.items():
            if not skill.last_used:
                continue

            last_used = datetime.fromisoformat(skill.last_used)
            days_since_use = (now - last_used).days

            old_state = skill.state

            if days_since_use > archive_days:
                if skill.state != "archived":
                    skill.state = "archived"
                    changes["archived"].append(name)
                    logger.info(f"Skill archived: {name} (unused for {days_since_use} days)")
            elif days_since_use > stale_days:
                if skill.state == "active":
                    skill.state = "stale"
                    changes["stale"].append(name)
                    logger.info(f"Skill marked stale: {name} (unused for {days_since_use} days)")
            else:
                if skill.state in ("stale", "archived"):
                    skill.state = "active"
                    changes["reactivated"].append(name)
                    logger.info(f"Skill reactivated: {name}")

        if any(changes.values()):
            self._save_skills()

        return changes

    def get_active_skills(self) -> List[SkillRecord]:
        """Obtiene habilidades activas"""
        return [s for s in self._skills.values() if s.state == "active"]

    def get_stale_skills(self) -> List[SkillRecord]:
        """Obtiene habilidades obsoletas"""
        return [s for s in self._skills.values() if s.state == "stale"]

    def get_archived_skills(self) -> List[SkillRecord]:
        """Obtiene habilidades archivadas"""
        return [s for s in self._skills.values() if s.state == "archived"]

    def get_skill(self, name: str) -> Optional[SkillRecord]:
        """Obtiene una habilidad especifica"""
        return self._skills.get(name)

    def list_skills(self, state: str = None) -> List[Dict]:
        """Lista habilidades con opcion de filtrar por estado"""
        skills = self._skills.values()
        if state:
            skills = [s for s in skills if s.state == state]

        return [{
            "name": s.name,
            "version": s.version,
            "description": s.description,
            "category": s.category,
            "state": s.state,
            "usage_count": s.usage_count,
            "last_used": s.last_used,
        } for s in sorted(skills, key=lambda x: x.usage_count, reverse=True)]

    def remove_skill(self, name: str) -> bool:
        """Elimina una habilidad"""
        if name in self._skills:
            del self._skills[name]
            self._save_skills()
            return True
        return False

    def get_stats(self) -> Dict:
        return {
            "total_skills": len(self._skills),
            "active": len(self.get_active_skills()),
            "stale": len(self.get_stale_skills()),
            "archived": len(self.get_archived_skills()),
            "total_usage": sum(s.usage_count for s in self._skills.values()),
            "categories": list(set(s.category for s in self._skills.values())),
        }

    def pin_skill(self, name: str) -> bool:
        """Fija una habilidad para que no sea archivada"""
        if name in self._skills:
            self._skills[name].state = "pinned"
            self._save_skills()
            return True
        return False

    def unpin_skill(self, name: str) -> bool:
        """Desfija una habilidad"""
        if name in self._skills and self._skills[name].state == "pinned":
            self._skills[name].state = "active"
            self._save_skills()
            return True
        return False

    def pause(self):
        """Pausa el curador automatico"""
        self._paused = True

    def resume(self):
        """Reanuda el curador automatico"""
        self._paused = False

    def maybe_run(self, is_idle: bool = False) -> Optional[Dict]:
        """Ejecuta revision de ciclo de vida si corresponde"""
        if self._paused:
            return None
        if not is_idle:
            return None
        self._run_count += 1
        changes = self.review_lifecycle()
        return {"run": self._run_count, "changes": changes}

    def get_status(self) -> Dict:
        """Obtiene estado completo del curador"""
        return {
            "total_skills": len(self._skills),
            "pinned_skills": len([s for s in self._skills.values() if s.state == "pinned"]),
            "active_skills": len(self.get_active_skills()),
            "stale_skills": len(self.get_stale_skills()),
            "archived_skills": len(self.get_archived_skills()),
            "paused": self._paused,
            "run_count": self._run_count,
        }

    def get_skill_report(self) -> List[Dict]:
        """Reporte detallado de todas las habilidades"""
        return sorted([
            {
                "name": s.name,
                "version": s.version,
                "category": s.category,
                "state": s.state,
                "usage_count": s.usage_count,
                "last_used": s.last_used,
                "tags": s.tags,
            }
            for s in self._skills.values()
        ], key=lambda x: x["usage_count"], reverse=True)
