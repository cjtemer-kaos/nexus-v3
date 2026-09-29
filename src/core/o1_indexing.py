"""
O(1) Indexing System - Sistema de índices O(1) para skills y gemas

Inspirado en indexing strategy:
- Role indexes (queenNode, coordinatorNode cached)
- Category index (Map<string, Set<string>>)
- Tag index (Map<string, Set<string>>)
- O(1) tool lookup via Map

Problema actual:
- Búsqueda de skills: O(n) scan lineal de 1,600+ skills
- Routing de gemas: O(n) keyword matching en tabla plana

Solución:
- Category index: {category: set(skill_names)} → O(1) lookup
- Tag index: {tag: set(skill_names)} → O(1) lookup
- Gema capability index: {capability: set(gema_names)} → O(1) routing
- Keyword index: {keyword: set(gema_names)} → O(1) routing

Performance gain: De O(n) a O(1) para búsquedas comunes
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class IndexStats:
    """Estadísticas de índices"""
    category_index_size: int = 0
    tag_index_size: int = 0
    gema_capability_index_size: int = 0
    gema_keyword_index_size: int = 0
    total_skills_indexed: int = 0
    total_gemas_indexed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    
    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0


class O1IndexManager:
    """
    Gestiona índices O(1) para skills y gemas.
    
    Inspirado en O(1) indexing strategy:
    - Role indexes para coordinadores
    - Category/tag indexes para tools
    - Bidirectional lookup maps
    
    Uso:
        index_mgr = O1IndexManager()
        index_mgr.build_skill_index(skill_loader)
        index_mgr.build_gema_index(director.gemas)
        
        # Búsquedas O(1)
        skills = index_mgr.get_skills_by_category("development")
        skills = index_mgr.get_skills_by_tag("react")
        gemas = index_mgr.get_gemas_by_capability("code-generation")
        gemas = index_mgr.get_gemas_by_keyword("python")
    """
    
    def __init__(self):
        # Skill indexes
        self.category_index: Dict[str, Set[str]] = defaultdict(set)
        self.tag_index: Dict[str, Set[str]] = defaultdict(set)
        self.skill_cache: Dict[str, str] = {}  # name → content cache
        
        # Gema indexes
        self.gema_capability_index: Dict[str, Set[str]] = defaultdict(set)
        self.gema_keyword_index: Dict[str, Set[str]] = defaultdict(set)
        
        # Stats
        self.stats = IndexStats()
    
    def build_skill_index(self, skill_loader) -> None:
        """
        Construir índices O(1) para skills desde ProgressiveSkillLoader.
        
        Complejidad: O(n) una vez, luego O(1) para búsquedas
        """
        try:
            # Acceder directamente a manifests (dict de SkillManifest)
            manifests = getattr(skill_loader, "manifests", {})
            
            for skill_name, manifest in manifests.items():
                # Indexar por categoría
                category = getattr(manifest, "category", "general")
                self.category_index[category].add(skill_name)
                
                # Indexar por tags/description keywords
                description = getattr(manifest, "description", "").lower()
                for word in description.split():
                    if len(word) > 3:
                        self.tag_index[word].add(skill_name)
                
                self.stats.total_skills_indexed += 1
            
            self.stats.category_index_size = len(self.category_index)
            self.stats.tag_index_size = len(self.tag_index)
            
            logger.info(f"Skill indexes built: {self.stats.category_index_size} categories, "
                       f"{self.stats.tag_index_size} tags, {self.stats.total_skills_indexed} skills")
        except Exception as e:
            logger.error(f"Failed to build skill index: {e}")
    
    def build_gema_index(self, gemas: Dict) -> None:
        """
        Construir índices O(1) para gemas.
        
        Complejidad: O(n) una vez, luego O(1) para routing
        """
        try:
            for gema_name, gema in gemas.items():
                # Indexar por capabilities (tags)
                for tag in gema.tags:
                    self.gema_capability_index[tag].add(gema_name)
                
                # Indexar por keywords (nombre + descripción)
                keywords = set()
                keywords.add(gema_name.lower())
                for word in gema.description.lower().split():
                    if len(word) > 3:
                        keywords.add(word)
                for keyword in keywords:
                    self.gema_keyword_index[keyword].add(gema_name)
                
                self.stats.total_gemas_indexed += 1
            
            self.stats.gema_capability_index_size = len(self.gema_capability_index)
            self.stats.gema_keyword_index_size = len(self.gema_keyword_index)
            
            logger.info(f"Gema indexes built: {self.stats.gema_capability_index_size} capabilities, "
                       f"{self.stats.gema_keyword_index_size} keywords, {self.stats.total_gemas_indexed} gemas")
        except Exception as e:
            logger.error(f"Failed to build gema index: {e}")
    
    def get_skills_by_category(self, category: str) -> List[str]:
        """O(1) lookup: obtener skills por categoría"""
        skills = self.category_index.get(category, set())
        if skills:
            self.stats.cache_hits += 1
        else:
            self.stats.cache_misses += 1
        return list(skills)
    
    def get_skills_by_tag(self, tag: str) -> List[str]:
        """O(1) lookup: obtener skills por tag"""
        skills = self.tag_index.get(tag, set())
        if skills:
            self.stats.cache_hits += 1
        else:
            self.stats.cache_misses += 1
        return list(skills)
    
    def get_gemas_by_capability(self, capability: str) -> List[str]:
        """O(1) lookup: obtener gemas por capacidad"""
        gemas = self.gema_capability_index.get(capability, set())
        if gemas:
            self.stats.cache_hits += 1
        else:
            self.stats.cache_misses += 1
        return list(gemas)
    
    def get_gemas_by_keyword(self, keyword: str) -> List[str]:
        """O(1) lookup: obtener gemas por keyword"""
        gemas = self.gema_keyword_index.get(keyword.lower(), set())
        if gemas:
            self.stats.cache_hits += 1
        else:
            self.stats.cache_misses += 1
        return list(gemas)
    
    def get_cached_skill(self, name: str, loader_fn) -> Optional[str]:
        """O(1) cache lookup para contenido de skill"""
        if name in self.skill_cache:
            self.stats.cache_hits += 1
            return self.skill_cache[name]
        
        self.stats.cache_misses += 1
        content = loader_fn(name) if loader_fn else None
        if content:
            self.skill_cache[name] = content
        return content
    
    def clear_skill_cache(self, max_size: int = 100) -> None:
        """Limpiar cache si excede tamaño máximo (LRU simple)"""
        if len(self.skill_cache) > max_size:
            # Eliminar 50% más antiguo (orden de inserción en dict)
            keys_to_remove = list(self.skill_cache.keys())[:max_size // 2]
            for key in keys_to_remove:
                del self.skill_cache[key]
            logger.info(f"Skill cache cleared: {len(self.skill_cache)} entries remaining")
    
    def get_stats(self) -> Dict:
        """Obtener estadísticas de índices"""
        return {
            "category_index_size": self.stats.category_index_size,
            "tag_index_size": self.stats.tag_index_size,
            "gema_capability_index_size": self.stats.gema_capability_index_size,
            "gema_keyword_index_size": self.stats.gema_keyword_index_size,
            "total_skills_indexed": self.stats.total_skills_indexed,
            "total_gemas_indexed": self.stats.total_gemas_indexed,
            "cache_hits": self.stats.cache_hits,
            "cache_misses": self.stats.cache_misses,
            "cache_hit_rate": round(self.stats.cache_hit_rate, 3),
            "skill_cache_size": len(self.skill_cache),
        }
