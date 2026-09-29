"""
Skills Marketplace — Registry searchable con metadata, install/publish, versioning.
"""
from __future__ import annotations
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

SKILL_REGISTRY_PATH = Path.home() / ".nexus" / "skill_registry.json"
SKILLS_INSTALL_DIR = Path(__file__).resolve().parent.parent.parent / "skills"


@dataclass
class SkillVersion:
    major: int = 0
    minor: int = 0
    patch: int = 0

    @classmethod
    def parse(cls, s: str) -> SkillVersion:
        parts = s.split(".")
        return cls(
            major=int(parts[0]) if len(parts) > 0 else 0,
            minor=int(parts[1]) if len(parts) > 1 else 0,
            patch=int(parts[2]) if len(parts) > 2 else 0,
        )

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __lt__(self, other: SkillVersion) -> bool:
        return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)

    def __le__(self, other: SkillVersion) -> bool:
        return (self.major, self.minor, self.patch) <= (other.major, other.minor, other.patch)

    def __gt__(self, other: SkillVersion) -> bool:
        return (self.major, self.minor, self.patch) > (other.major, other.minor, other.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SkillVersion):
            return NotImplemented
        return (self.major, self.minor, self.patch) == (other.major, other.minor, other.patch)


@dataclass
class SkillManifest:
    name: str
    version: str = "1.0.0"
    author: str = ""
    tags: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    description: str = ""
    rating: float = 0.0
    rating_count: int = 0
    install_path: str = ""
    published_at: str = ""

    def version_obj(self) -> SkillVersion:
        return SkillVersion.parse(self.version)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "version": self.version, "author": self.author,
            "tags": self.tags, "requires": self.requires, "description": self.description,
            "rating": self.rating, "rating_count": self.rating_count,
            "install_path": self.install_path, "published_at": self.published_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SkillManifest:
        return cls(**{k: d.get(k, v.default if hasattr(v, 'default') else "")
                      for k, v in cls.__dataclass_fields__.items()})


class SkillRegistry:
    """Registry of available skills with search, install, publish."""

    def __init__(self, storage_path: str | None = None):
        self._path = Path(storage_path or str(SKILL_REGISTRY_PATH))
        self._skills: dict[str, SkillManifest] = {}
        self._load()

    def register(self, manifest: SkillManifest) -> None:
        self._skills[manifest.name] = manifest
        self._save()

    def get(self, name: str) -> SkillManifest | None:
        return self._skills.get(name)

    def search(self, query: str) -> list[SkillManifest]:
        q = query.lower()
        results = []
        for skill in self._skills.values():
            if q in skill.name.lower() or q in skill.description.lower() or q in [t.lower() for t in skill.tags]:
                results.append(skill)
        return results

    def search_by_tag(self, tag: str) -> list[SkillManifest]:
        return [s for s in self._skills.values() if tag.lower() in [t.lower() for t in s.tags]]

    def list_by_rating(self, top_k: int = 10) -> list[SkillManifest]:
        sorted_skills = sorted(self._skills.values(), key=lambda s: s.rating, reverse=True)
        return sorted_skills[:top_k]

    def update_rating(self, name: str, new_rating: float) -> SkillManifest | None:
        skill = self._skills.get(name)
        if not skill:
            return None
        skill.rating = ((skill.rating * skill.rating_count) + new_rating) / (skill.rating_count + 1)
        skill.rating_count += 1
        self._save()
        return skill

    def install(self, name: str) -> bool:
        """Copy skill from registry to local skills directory."""
        skill = self._skills.get(name)
        if not skill or not skill.install_path:
            return False
        src = Path(skill.install_path)
        if not src.exists():
            return False
        dst = SKILLS_INSTALL_DIR / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        logger.info(f"Skill installed: {name} -> {dst}")
        return True

    def publish(self, manifest: SkillManifest) -> None:
        """Register or update a skill."""
        existing = self._skills.get(manifest.name)
        if existing:
            existing_v = existing.version_obj()
            new_v = manifest.version_obj()
            if new_v <= existing_v:
                raise ValueError(f"Version {manifest.version} <= existing {existing.version}")
        self.register(manifest)

    @property
    def skills(self) -> list[SkillManifest]:
        return list(self._skills.values())

    def status(self) -> dict:
        return {"total": len(self._skills), "avg_rating": round(
            sum(s.rating for s in self._skills.values()) / max(len(self._skills), 1), 2)}

    # Alias for API consistency (other registries expose get_stats)
    def get_stats(self) -> dict:
        return self.status()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {name: m.to_dict() for name, m in self._skills.items()}
        atomic_write_json(self._path, data)

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    data = json.load(f)
                for name, d in data.items():
                    self._skills[name] = SkillManifest.from_dict(d)
            except (json.JSONDecodeError, OSError):
                pass
