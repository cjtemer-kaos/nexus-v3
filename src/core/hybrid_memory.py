"""
Hybrid Memory Backend - FTS5 + Vector Search + Entity Boost

Arquitectura:
- FTS5: Busqueda por keywords exactas (BM25)
- Vector Search: Busqueda semantica con embeddings de Ollama
- Entity Boost: Extraccion de entidades + boosting por relevancia
- Hybrid: Fusion RRF de FTS5 + semantica, con entity boost persistido

Pipeline de scoring (fix #5/#6/#7):
  1. FTS5 y semantica con overfetch (limit*2)
  2. Reciprocal Rank Fusion: rrf = SUM 1/(K + rank), K=60
  3. Normalizacion rrf_norm = rrf * (K+1)/2  -> [0, 1]
  4. Entity boost (tabla memory_entities persistida)
  5. Orden por hybrid_score desc, sin gating por umbral
"""

from src.brain.link import brain_path
import json
import logging
import re
import sqlite3
from contextlib import closing
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """Entrada de memoria hibrida"""
    id: Optional[int] = None
    content: str = ""
    category: str = "general"
    topic_key: str = ""
    embedding: Optional[List[float]] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    source: str = "agent"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    """Resultado de busqueda hibrida"""
    entry: MemoryEntry
    fts_score: float = 0.0
    semantic_score: float = 0.0
    entity_boost: float = 0.0
    hybrid_score: float = 0.0
    source: str = "fts"


class HybridMemoryBackend:
    """
    Backend de memoria hibrida: FTS5 + Vector Search + Entity Boost.
    
    Uso:
        backend = HybridMemoryBackend()
        backend.add("El usuario prefiere Python sobre JavaScript", "preference", "user_prefs")
        results = backend.search("que lenguaje le gusta al usuario", strategy="hybrid")
    """
    
    ENTITY_BOOST_WEIGHT = 0.5
    SEMANTIC_WEIGHT = 1.0
    FTS_WEIGHT = 0.5
    MIN_SEMANTIC_THRESHOLD = 0.3
    
    def __init__(self, db_path: Path = None, ollama_url: str = "http://localhost:11434"):
        self.db_path = db_path or brain_path() / "hybrid_memory.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ollama_url = ollama_url
        self.embedding_model = "nomic-embed-text"
        self.embedding_cache: Dict[str, List[float]] = {}
        self._entity_index: Dict[str, List[int]] = {}  # entity_text -> [memory_ids]
        self._init_db()
        self._load_entity_index()
    
    def _init_db(self):
        """Inicializar base de datos con FTS5"""
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            
            # Tabla principal
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    topic_key TEXT DEFAULT '',
                    embedding TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    source TEXT DEFAULT 'agent',
                    metadata TEXT DEFAULT '{}'
                )
            """)
            
            # FTS5 virtual table
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content, category, topic_key,
                    content='memories', content_rowid='id'
                )
            """)
            
            # Triggers para FTS5
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
                BEGIN
                    INSERT INTO memories_fts(rowid, content, category, topic_key)
                    VALUES (new.id, new.content, new.category, new.topic_key);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories
                BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, category, topic_key)
                    VALUES ('delete', old.id, old.content, old.category, old.topic_key);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories
                BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, category, topic_key)
                    VALUES ('delete', old.id, old.content, old.category, old.topic_key);
                    INSERT INTO memories_fts(rowid, content, category, topic_key)
                    VALUES (new.id, new.content, new.category, new.topic_key);
                END
            """)
            
            # Índice para topic_key
            conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_key ON memories(topic_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON memories(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON memories(created_at)")

            # Índice de entidades persistido (fix #7: antes solo vivía en memoria)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_entities (
                    memory_id INTEGER NOT NULL,
                    entity TEXT NOT NULL,
                    PRIMARY KEY (memory_id, entity)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_entities_entity"
                " ON memory_entities(entity)"
            )
    
    def _get_embedding(self, text: str) -> List[float]:
        """Obtener embedding de Ollama (con cache)"""
        # Cache key
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in self.embedding_cache:
            return self.embedding_cache[cache_key]
        
        try:
            response = httpx.post(
                f"{self.ollama_url}/api/embeddings",
                json={"model": self.embedding_model, "prompt": text},
                timeout=30.0,
            )
            if response.status_code == 200:
                embedding = response.json().get("embedding", [])
                self.embedding_cache[cache_key] = embedding
                return embedding
        except Exception as e:
            logger.warning(f"Failed to get embedding: {e}")
        
        return []
    
    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Calcular similitud coseno entre dos vectores"""
        if not a or not b:
            return 0.0
        
        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        
        if norm_a == 0 or norm_b == 0:
            return 0.0
        
        return dot_product / (norm_a * norm_b)
    
    def _extract_entities(self, text: str) -> List[str]:
        entities = []
        parts = re.split(r'[\s,.;:!?()\[\]{}"\'«»]+', text)
        for p in parts:
            p = p.strip()
            if not p or len(p) < 3:
                continue
            if p[0].isupper() and p[0].isalpha():
                entities.append(p.lower())
        unique = list(dict.fromkeys(entities))
        return unique[:20]

    def _load_entity_index(self):
        """Cargar índice de entidades persistido (fix #7)."""
        try:
            with closing(sqlite3.connect(str(self.db_path))) as conn:
                rows = conn.execute(
                    "SELECT entity, memory_id FROM memory_entities ORDER BY memory_id"
                ).fetchall()
            index: Dict[str, List[int]] = {}
            for ent, mid in rows:
                index.setdefault(ent, []).append(int(mid))
            self._entity_index = index
        except Exception as e:
            logger.warning(f"Failed to load entity index: {e}")

    def _update_entity_index(self, memory_id: int, content: str):
        entities = self._extract_entities(content)
        # persistir (fix #7) — DELETE+INSERT mantiene la tabla en sync
        try:
            with closing(sqlite3.connect(str(self.db_path))) as conn:
                conn.execute(
                    "DELETE FROM memory_entities WHERE memory_id=?", (memory_id,)
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO memory_entities (memory_id, entity)"
                    " VALUES (?, ?)",
                    [(memory_id, e) for e in entities],
                )
        except Exception as e:
            logger.warning(f"Failed to persist entity index: {e}")
        # sync dict: quitar memory_id de todas las listas y re-agregar
        for ids in self._entity_index.values():
            if memory_id in ids:
                ids[:] = [i for i in ids if i != memory_id]
        for ent in entities:
            self._entity_index.setdefault(ent, []).append(memory_id)

    def _compute_entity_boost(self, query: str, memory_id: int) -> float:
        query_entities = self._extract_entities(query)
        if not query_entities:
            return 0.0
        boost = 0.0
        for ent in query_entities:
            linked = self._entity_index.get(ent, [])
            if memory_id in linked:
                weight = 1.0 / (1.0 + 0.001 * ((len(linked) - 1) ** 2))
                boost += self.ENTITY_BOOST_WEIGHT * weight
        return min(boost, 1.0)

    def add(self, content: str, category: str = "general", topic_key: str = "", source: str = "agent", metadata: Dict = None) -> int:
        """Agregar entrada de memoria con indexacion de entidades"""
        embedding = self._get_embedding(content)
        embedding_json = json.dumps(embedding) if embedding else None
        
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO memories (content, category, topic_key, embedding, source, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (content, category, topic_key, embedding_json, source, json.dumps(metadata or {}))
            )
            memory_id = cursor.lastrowid
        
        self._update_entity_index(memory_id, content)
        return memory_id

    def upsert(self, content: str, category: str = "general", topic_key: str = "",
               source: str = "agent", metadata: Dict = None) -> int:
        """Insertar o actualizar una memoria por (topic_key, category).

        Sin topic_key conserva la semántica append-only de ``add``.
        """
        if not topic_key:
            return self.add(content, category, topic_key, source, metadata)

        embedding = self._get_embedding(content)
        embedding_json = json.dumps(embedding) if embedding else None
        with closing(sqlite3.connect(str(self.db_path), timeout=10.0)) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, content FROM memories WHERE topic_key=? AND category=? "
                "ORDER BY id LIMIT 1",
                (topic_key, category),
            ).fetchone()
            if row:
                memory_id = int(row["id"])
                conn.execute(
                    "UPDATE memories SET content=?, embedding=?, source=?, metadata=? WHERE id=?",
                    (content, embedding_json, source, json.dumps(metadata or {}), memory_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO memories (content, category, topic_key, embedding, source, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (content, category, topic_key, embedding_json, source,
                     json.dumps(metadata or {})),
                )
                memory_id = int(cur.lastrowid)
            conn.commit()

        self._update_entity_index(memory_id, content)
        return memory_id
    
    def search_fts(self, query: str, limit: int = 10) -> List[SearchResult]:
        """Búsqueda FTS5 (keyword exacta)"""
        results = []
        try:
            with closing(sqlite3.connect(str(self.db_path))) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT m.*, bm25(memories_fts) as score FROM memories m JOIN memories_fts f ON m.id = f.rowid WHERE memories_fts MATCH ? ORDER BY score LIMIT ?",
                    (query, limit)
                )
                for row in cursor.fetchall():
                    entry = MemoryEntry(
                        id=row["id"],
                        content=row["content"],
                        category=row["category"],
                        topic_key=row["topic_key"],
                        created_at=row["created_at"],
                        source=row["source"],
                        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                    )
                    results.append(SearchResult(
                        entry=entry,
                        fts_score=abs(row["score"]) if row["score"] else 0,
                        source="fts",
                    ))
        except Exception as e:
            logger.warning(f"FTS5 search failed: {e}")
        
        return results
    
    def search_semantic(self, query: str, limit: int = 10) -> List[SearchResult]:
        """Busqueda semantica con embeddings + threshold gating"""
        query_embedding = self._get_embedding(query)
        if not query_embedding:
            return []
        
        overfetch = limit * 4
        results = []
        try:
            with closing(sqlite3.connect(str(self.db_path))) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT id, content, category, topic_key, embedding, created_at, source, metadata FROM memories ORDER BY id DESC LIMIT ?", (overfetch,))
                
                for row in cursor.fetchall():
                    if row["embedding"]:
                        entry_embedding = json.loads(row["embedding"])
                        similarity = self._cosine_similarity(query_embedding, entry_embedding)
                        
                        if similarity < self.MIN_SEMANTIC_THRESHOLD:
                            continue
                        
                        entry = MemoryEntry(
                            id=row["id"],
                            content=row["content"],
                            category=row["category"],
                            topic_key=row["topic_key"],
                            created_at=row["created_at"],
                            source=row["source"],
                            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                        )
                        results.append(SearchResult(
                            entry=entry,
                            semantic_score=similarity,
                            source="semantic",
                        ))
                
                results.sort(key=lambda r: r.semantic_score, reverse=True)
                return results[:limit]
        except Exception as e:
            logger.warning(f"Semantic search failed: {e}")
        
        return []
    
    def search_hybrid(self, query: str, limit: int = 10) -> List[SearchResult]:
        """
        Busqueda hibrida con fusion RRF (Reciprocal Rank Fusion) + entity boost.

        Pipeline (fix #5/#6/#7):
          1. Overfetch (limit*2) en FTS5 y semantica
          2. RRF: rrf = SUM 1/(K + rank), K = 60
          3. Normalizacion rrf_norm = rrf * (K+1)/2  -> [0, 1]
          4. + entity_boost, clamp a 1.0
          5. Orden por hybrid_score desc (sin gating de umbral)
        """
        RRF_K = 60
        fts_results = self.search_fts(query, limit=limit * 2)
        semantic_results = self.search_semantic(query, limit=limit * 2)
        
        merged: Dict[int, SearchResult] = {}
        
        for result in fts_results:
            eid = result.entry.id
            merged[eid] = SearchResult(
                entry=result.entry,
                fts_score=result.fts_score,
                source="hybrid",
            )
        
        for result in semantic_results:
            eid = result.entry.id
            if eid in merged:
                merged[eid].semantic_score = result.semantic_score
            else:
                merged[eid] = SearchResult(
                    entry=result.entry,
                    semantic_score=result.semantic_score,
                    source="hybrid",
                )
        
        # RRF: rank 1 = mejor; listas presentes en ambas -> rrf_norm hasta 1.0
        rrf: Dict[int, float] = {}
        for rank, result in enumerate(fts_results, start=1):
            rrf[result.entry.id] = rrf.get(result.entry.id, 0.0) + 1.0 / (RRF_K + rank)
        for rank, result in enumerate(semantic_results, start=1):
            rrf[result.entry.id] = rrf.get(result.entry.id, 0.0) + 1.0 / (RRF_K + rank)
        norm = (RRF_K + 1) / 2.0

        candidates = list(merged.values())
        for c in candidates:
            rrf_norm = rrf.get(c.entry.id, 0.0) * norm
            boost = self._compute_entity_boost(query, c.entry.id)
            c.entity_boost = boost
            c.hybrid_score = min(rrf_norm + boost, 1.0)
        
        candidates.sort(key=lambda r: r.hybrid_score, reverse=True)
        return candidates[:limit]
    
    def search(self, query: str, strategy: str = "auto", limit: int = 10) -> List[SearchResult]:
        """
        Búsqueda inteligente con routing automático.
        
        Estrategias:
        - "auto": Routing basado en tipo de query
        - "fts": Solo FTS5
        - "semantic": Solo semantic
        - "hybrid": Combina ambos con RRF
        """
        if strategy == "auto":
            # Routing: queries cortas con keywords -> FTS5, queries largas -> hybrid
            if len(query.split()) <= 3:
                strategy = "fts"
            else:
                strategy = "hybrid"
        
        if strategy == "fts":
            return self.search_fts(query, limit)
        elif strategy == "semantic":
            return self.search_semantic(query, limit)
        elif strategy == "hybrid":
            return self.search_hybrid(query, limit)
        else:
            return self.search_fts(query, limit)
    
    def get_by_topic(self, topic_key: str) -> List[MemoryEntry]:
        """Obtener memorias por topic key"""
        results = []
        try:
            with closing(sqlite3.connect(str(self.db_path))) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM memories WHERE topic_key = ? ORDER BY created_at DESC", (topic_key,))
                for row in cursor.fetchall():
                    results.append(MemoryEntry(
                        id=row["id"],
                        content=row["content"],
                        category=row["category"],
                        topic_key=row["topic_key"],
                        created_at=row["created_at"],
                        source=row["source"],
                        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                    ))
        except Exception as e:
            logger.warning(f"Failed to get by topic: {e}")
        
        return results
    
    def delete(self, memory_id: int) -> bool:
        """Eliminar memoria (soft delete)"""
        try:
            with closing(sqlite3.connect(str(self.db_path))) as conn:
                conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
                conn.execute(
                    "DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,)
                )
            for ids in self._entity_index.values():
                if memory_id in ids:
                    ids[:] = [i for i in ids if i != memory_id]
            return True
        except Exception as e:
            logger.warning(f"Failed to delete memory: {e}")
            return False
    
    def get_stats(self) -> Dict:
        """Obtener estadísticas"""
        try:
            with closing(sqlite3.connect(str(self.db_path))) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM memories")
                total = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL")
                with_embeddings = cursor.fetchone()[0]
                cursor.execute("SELECT DISTINCT category FROM memories")
                categories = [r[0] for r in cursor.fetchall()]
                
                return {
                    "total_memories": total,
                    "with_embeddings": with_embeddings,
                    "categories": categories,
                    "embedding_cache_size": len(self.embedding_cache),
                    "db_path": str(self.db_path),
                }
        except Exception as e:
            return {"error": str(e)}
