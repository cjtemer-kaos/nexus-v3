import json
import logging
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

class SkillGateway:
    """
    Gateway para acceder a las habilidades externas
    """

    def __init__(self, skills_root: str = None):
        if skills_root is None:
            skills_root = os.path.join(os.path.dirname(__file__), "..", "skills", "hub")
        self.root = Path(skills_root)
        self.index_path = self.root / "full_skills_map.json"
        self.skills_cache = []
        self._load_index()

    def _load_index(self):
        if self.index_path.exists():
            try:
                with open(self.index_path, 'r', encoding='utf-8') as f:
                    self.skills_cache = json.load(f)
            except Exception as e:
                logger.error(f"Error loading skills index: {e}")

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
        """
        skill_dir = self.root / name
        if not skill_dir.exists():
            return {"error": f"Skill '{name}' not found."}

        info = {
            "name": name,
            "path": str(skill_dir),
            "files": [f.name for f in skill_dir.iterdir() if f.is_file()],
            "subdirs": [d.name for d in skill_dir.iterdir() if d.is_dir()],
            "readme": ""
        }

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
