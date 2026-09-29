"""Agency Catalog — lazy-loading catalog of 266 specialized AI agents from agency-agents.

Pattern: lightweight JSON catalog for routing (~144KB), full .md prompts loaded on demand (~4MB total).
Inspired by msitarzewski/agency-agents (136k stars).

Usage:
    catalog = AgencyCatalog()
    # Search by query
    results = catalog.search("security vulnerability scanning", top_k=5)
    # Load full prompt for a specific agent
    prompt = catalog.load_prompt("engineering-incident-response-commander")
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


class AgencyCatalog:
    """Lazy-loading catalog of specialized AI agents."""

    def __init__(self, catalog_path: str = "", agents_dir: str = ""):
        # Resolve paths relative to project root
        if not catalog_path:
            project_root = Path(__file__).parent.parent.parent
            catalog_path = str(project_root / "data" / "agency_catalog.json")
        if not agents_dir:
            project_root = Path(__file__).parent.parent.parent
            agents_dir = str(project_root / "data" / "agency_agents")

        self.catalog_path = Path(catalog_path)
        self.agents_dir = Path(agents_dir)
        self._catalog: List[Dict[str, Any]] = []
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        if self.catalog_path.exists():
            self._catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        self._loaded = True

    def search(self, query: str, top_k: int = 5, division: str = "") -> List[Dict[str, Any]]:
        """Search catalog by query. Returns top_k matches with name, emoji, description, division, score."""
        self._ensure_loaded()

        query_lower = query.lower()
        query_words = set(query_lower.split())

        scored = []
        for agent in self._catalog:
            score = 0.0

            # Division filter
            if division and agent.get("division") != division:
                continue

            # Name match (highest weight)
            name_lower = agent.get("name", "").lower()
            if query_lower in name_lower:
                score += 3.0
            for word in query_words:
                if word in name_lower:
                    score += 1.0

            # Description match
            desc_lower = agent.get("description", "").lower()
            for word in query_words:
                if word in desc_lower:
                    score += 0.5

            # Tag match (medium weight)
            tags = agent.get("tags", [])
            for tag in tags:
                if tag in query_lower:
                    score += 1.5
                for word in query_words:
                    if word in tag:
                        score += 0.3

            # Emoji/description bonus
            if score > 0:
                scored.append({**agent, "score": score})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def load_prompt(self, agent_name: str) -> Optional[str]:
        """Load full .md prompt for an agent by name."""
        self._ensure_loaded()

        # Find agent in catalog
        for agent in self._catalog:
            if agent.get("name") == agent_name:
                file_path = agent.get("file", "")
                if file_path and Path(file_path).exists():
                    return Path(file_path).read_text(encoding="utf-8")
                # Try by filename pattern
                for md_file in self.agents_dir.rglob("*.md"):
                    if agent_name.lower() in md_file.stem.lower():
                        return md_file.read_text(encoding="utf-8")
                break

        # Fallback: search in agents_dir
        if self.agents_dir.exists():
            for md_file in self.agents_dir.rglob("*.md"):
                if agent_name.lower() in md_file.stem.lower():
                    return md_file.read_text(encoding="utf-8")

        return None

    def list_divisions(self) -> Dict[str, int]:
        """List all divisions with agent counts."""
        self._ensure_loaded()
        from collections import Counter
        return dict(Counter(a.get("division", "unknown") for a in self._catalog))

    def list_agents(self, division: str = "") -> List[Dict[str, Any]]:
        """List all agents, optionally filtered by division."""
        self._ensure_loaded()
        if division:
            return [a for a in self._catalog if a.get("division") == division]
        return self._catalog

    def get_stats(self) -> Dict[str, Any]:
        """Get catalog statistics."""
        self._ensure_loaded()
        from collections import Counter
        divisions = Counter(a.get("division", "unknown") for a in self._catalog)
        return {
            "total_agents": len(self._catalog),
            "divisions": dict(divisions),
            "catalog_size_kb": self.catalog_path.stat().st_size // 1024 if self.catalog_path.exists() else 0,
        }


# Singleton
_catalog_instance: Optional[AgencyCatalog] = None


def get_catalog() -> AgencyCatalog:
    global _catalog_instance
    if _catalog_instance is None:
        _catalog_instance = AgencyCatalog()
    return _catalog_instance
