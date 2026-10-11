import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

try:
    from src.core.skill_frontmatter import merge_by_rank, scan_roots
    _FRONTMATTER_AVAILABLE = True
except Exception:  # pragma: no cover - fallback al indice viejo
    merge_by_rank = None  # type: ignore
    scan_roots = None  # type: ignore
    _FRONTMATTER_AVAILABLE = False


def _default_roots(skills_root: Path) -> List[str]:
    """Roots de skills: config `skills.roots` > data/skills > skills_root viejo."""
    try:
        from src.core.config import load_config

        cfg = load_config()
        roots = (cfg.get("skills") or {}).get("roots")
        if roots:
            return [str(r) for r in roots]
    except Exception:
        pass
    return [str(Path("data") / "skills"), str(skills_root)]

class SkillGateway:
    """
    Gateway para acceder a las habilidades externas
    """

    def __init__(self, skills_root: str = None, roots: Optional[Sequence[str]] = None,
                 watch: bool = False):
        if skills_root is None:
            skills_root = os.path.join(os.path.dirname(__file__), "..", "skills", "hub")
        self.root = Path(skills_root)
        self.index_path = self.root / "full_skills_map.json"
        self.skills_cache = []
        # Roots con rank (menor rank = mas prioridad). `watch` se guarda como
        # flag documentado (el watcher vivo es opt-in externo, no se arranca aqui).
        self.roots: List[str] = list(roots) if roots else _default_roots(self.root)
        self.watch = watch
        self._frontmatter_errors: List[Dict] = []
        self._load_index()
        self._merge_frontmatter()

    def _load_index(self):
        if self.index_path.exists():
            try:
                with open(self.index_path, 'r', encoding='utf-8') as f:
                    self.skills_cache = json.load(f)
            except Exception as e:
                logger.error(f"Error loading skills index: {e}")

    def _merge_frontmatter(self):
        """Lee frontmatter real cuando existe; fallback al indice viejo si no.

        Compat: si no hay PyYAML, no hay roots con SKILL.md, o todo es
        invalido, `skills_cache` queda como lo dejo `_load_index` (sin romper).
        A igual `name`, el frontmatter gana al indice segun rank del root.
        """
        if not _FRONTMATTER_AVAILABLE or not self.roots:
            return
        try:
            found, errors = scan_roots(self.roots)
        except Exception as e:
            logger.error(f"Error scanning skill roots: {e}")
            return
        self._frontmatter_errors = errors
        if not found:
            return
        merged_entries = merge_by_rank(found)
        by_name: Dict[str, Dict] = {s.get("name"): dict(s) for s in self.skills_cache if s.get("name")}
        for e in merged_entries:
            prev = by_name.get(e["name"], {})
            skill = {
                "name": e["name"],
                "description": e.get("description", prev.get("description", "")),
                "category": prev.get("category", "skills"),
                "tags": prev.get("tags", ""),
                "path": e.get("path", ""),
                "root": e.get("root", ""),
                "rank": e.get("rank", 0),
                "frontmatter": True,
            }
            if "whenToUse" in e:
                skill["whenToUse"] = e["whenToUse"]
            if "metadata" in e:
                skill["metadata"] = e["metadata"]
            # Preserva claves extra del indice viejo (compat).
            for k, v in prev.items():
                skill.setdefault(k, v)
            by_name[e["name"]] = skill
        self.skills_cache = sorted(by_name.values(), key=lambda s: s.get("name", ""))

    def frontmatter_errors(self) -> List[Dict]:
        """Errores de parseo de frontmatter del ultimo scan (diagnostico)."""
        return list(self._frontmatter_errors)

    def search_skills(self, query: str, limit: int = 10) -> List[Dict]:
        """
        Busca habilidades por nombre o descripcion.
        """
        query = query.lower()
        results = []
        for skill in self.skills_cache:
            if query in skill['name'].lower() or query in skill['description'].lower():
                results.append(skill)
                if len(results) >= limit:
                    break
        return results

    def get_skill_info(self, name: str) -> Dict:
        """
        Obtiene informacion detallada de una habilidad especifica.

        Si existe SKILL.md con frontmatter (en el root viejo o en alguno de
        `self.roots`), se incluye `frontmatter` + `body_preview`; si no,
        fallback al README viejo (compat).
        """
        skill_dir = self.root / name
        cached = next((s for s in self.skills_cache if s.get("name") == name), {})
        if cached.get("path"):
            skill_dir = Path(cached["path"]).parent
        if not skill_dir.exists():
            return {"error": f"Skill '{name}' not found."}

        info = {
            "name": name,
            "path": str(skill_dir),
            "files": [f.name for f in skill_dir.iterdir() if f.is_file()],
            "subdirs": [d.name for d in skill_dir.iterdir() if d.is_dir()],
            "readme": ""
        }

        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists() and _FRONTMATTER_AVAILABLE:
            try:
                from src.core.skill_frontmatter import parse_frontmatter_file

                parsed = parse_frontmatter_file(skill_md)
                if parsed["valid"]:
                    info["frontmatter"] = parsed["frontmatter"]
                    info["body_preview"] = parsed["body"][:2000]
            except Exception:
                pass

        readme_path = skill_dir / "README.md"
        if readme_path.exists():
            try:
                info["readme"] = readme_path.read_text(encoding="utf-8")[:2000]
            except:
                pass

        return info

    def list_categories(self) -> Dict[str, int]:
        """
        Lista todas las categorias (prefijos) y su conteo.
        """
        categories = {}
        for skill in self.skills_cache:
            cat = skill['category']
            categories[cat] = categories.get(cat, 0) + 1
        return dict(sorted(categories.items(), key=lambda x: x[1], reverse=True))

    def suggest_skill(self, task_description: str) -> List[Dict]:
        """
        Sugiere habilidades basadas en una descripcion de tarea (heuristica basica).
        """
        # Implementacion simplificada: buscar palabras clave de la tarea en los nombres de los skills
        words = task_description.lower().split()
        suggestions = []
        for word in words:
            if len(word) > 3:
                suggestions.extend(self.search_skills(word, limit=3))
        
        # Eliminar duplicados
        seen = set()
        unique_suggestions = []
        for s in suggestions:
            if s['name'] not in seen:
                unique_suggestions.append(s)
                seen.add(s['name'])
        
        return unique_suggestions[:5]
