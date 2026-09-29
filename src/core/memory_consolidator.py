"""
MemoryConsolidator - 3-Tier Memory Pipeline (FTS5 pattern + ADD-only + Entity Linking)

Fusiona:
- Claude Memory Tool: persistent memory across sessions, progressive disclosure
- mem0 V3: Entity linking, entity graph, relationship discovery

3 fases:
1. Selection: Filtra mensajes que valen la pena recordar
2. Extraction: Extrae facts como JSON {fact, category, confidence, source, entity_links}
3. Consolidation: Mergea con DB existente, dedup por topic_key + similitud FTS5
4. Entity Linking: Auto-descubre relaciones entre entidades mencionadas

ADD-only: Los hechos se acumulan cronológicamente. Resolución de conflictos
en retrieval, no en escritura.

Entity Linking (mem0 V3 pattern):
- Entity Registry: Track all known entities across memories
- Entity Graph: Store entity→entity relationships with weights
- Auto-discovery: Extract entity co-occurrences from facts
- Entity-aware Search: Boost results by entity connections
"""

import json
import logging
import sqlite3
import time
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("nexus-memory")


@dataclass
class MemoryFact:
    fact: str
    category: str
    confidence: float
    source: str
    timestamp: float = field(default_factory=time.time)
    topic_key: str = ""
    entity_links: List[str] = field(default_factory=list)


class MemoryConsolidator:
    """
    3-tier memory pipeline con patrón FTS5 + ADD-only.
    """

    TRIVIAL_PATTERNS = [
        r"^(hello|hi|hey|greetings|good\s+(morning|afternoon|evening))",
        r"^(thanks|thank\s*you|thx|appreciate)",
        r"^(ok|okay|sure|yes|no|right|got\s*it)",
        r"^(bye|goodbye|see\s*you|later)",
    ]

    def __init__(
        self,
        db_path: str = None,
        ollama_url: str = "http://localhost:11434",
        extraction_model: str = "qwen2.5:0.5b",
        similarity_threshold: float = 0.85,
        max_memories: int = 200,
    ):
        if db_path is None:
            db_path = str(Path.home() / ".nexus" / "brain" / "cerebro.db")
        self.db_path = db_path
        self.ollama_url = ollama_url
        self.extraction_model = extraction_model
        self.similarity_threshold = similarity_threshold
        self.max_memories = max_memories
        self._ollama_available: Optional[bool] = None
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            confidence REAL DEFAULT 0.5,
            source TEXT DEFAULT '',
            timestamp REAL DEFAULT (strftime('%s', 'now')),
            topic_key TEXT DEFAULT '',
            entity_links TEXT DEFAULT '[]',
            is_active INTEGER DEFAULT 1,
            revision_count INTEGER DEFAULT 1,
            normalized_hash TEXT DEFAULT '',
            duplicate_count INTEGER DEFAULT 0,
            last_seen_at REAL DEFAULT (strftime('%s', 'now')),
            deleted_at REAL DEFAULT NULL
        )""")
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts 
            USING fts5(fact, category, source, content='memories', content_rowid='id')""")

        # Migración: añadir columnas faltantes en DBs existentes
        for col_def in [
            ("revision_count", "INTEGER DEFAULT 1"),
            ("normalized_hash", "TEXT DEFAULT ''"),
            ("duplicate_count", "INTEGER DEFAULT 0"),
            ("last_seen_at", "REAL DEFAULT (strftime('%s', 'now'))"),
            ("deleted_at", "REAL DEFAULT NULL"),
        ]:
            try:
                c.execute(f"ALTER TABLE memories ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # columna ya existe

        # Memory relations (conflict/supersede/related)
        c.execute("""CREATE TABLE IF NOT EXISTS memory_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL,
            reason TEXT DEFAULT '',
            created_at REAL DEFAULT (strftime('%s', 'now')),
            FOREIGN KEY (source_id) REFERENCES memories(id),
            FOREIGN KEY (target_id) REFERENCES memories(id),
            UNIQUE(source_id, target_id, relation_type)
        )""")

        # Entity Registry
        c.execute("""CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            entity_type TEXT DEFAULT 'concept',
            mention_count INTEGER DEFAULT 1,
            first_seen REAL DEFAULT (strftime('%s', 'now')),
            last_seen REAL DEFAULT (strftime('%s', 'now')),
            metadata TEXT DEFAULT '{}'
        )""")

        # Entity Graph
        c.execute("""CREATE TABLE IF NOT EXISTS entity_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_entity TEXT NOT NULL,
            target_entity TEXT NOT NULL,
            relation_type TEXT DEFAULT 'co_occurrence',
            weight REAL DEFAULT 1.0,
            evidence TEXT DEFAULT '',
            created_at REAL DEFAULT (strftime('%s', 'now')),
            UNIQUE(source_entity, target_entity, relation_type)
        )""")

        # Entity-Memory mapping
        c.execute("""CREATE TABLE IF NOT EXISTS entity_memory_map (
            entity_id INTEGER NOT NULL,
            memory_id INTEGER NOT NULL,
            relevance REAL DEFAULT 1.0,
            PRIMARY KEY (entity_id, memory_id),
            FOREIGN KEY (entity_id) REFERENCES entities(id),
            FOREIGN KEY (memory_id) REFERENCES memories(id)
        )""")

        conn.commit()
        conn.close()

    def suggest_topic_key(self, text: str) -> str:
        words = re.findall(r'[a-zA-Z0-9]+', text.lower())
        stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'in', 'on',
                      'at', 'to', 'for', 'of', 'with', 'and', 'or', 'not', 'this',
                      'that', 'it', 'be', 'has', 'have', 'do', 'does', 'did'}
        filtered = [w for w in words if w not in stop_words and len(w) > 2]
        meaningful = filtered[:4]
        if len(meaningful) >= 2:
            return '-'.join(meaningful)
        return '-'.join(filtered[:3]) if filtered else text.lower().replace(' ', '-')[:40]

    def _compute_hash(self, fact: str, topic_key: str) -> str:
        raw = f"{fact.strip().lower()}|{topic_key}"
        return __import__("hashlib").md5(raw.encode()).hexdigest()

    async def select(self, messages: List[Dict]) -> List[str]:
        """Filtra mensajes que vale la pena recordar."""
        worthy = []
        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, str) or len(content.strip()) < 20:
                continue
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            is_trivial = False
            for pattern in self.TRIVIAL_PATTERNS:
                if __import__("re").search(pattern, content, __import__("re").IGNORECASE):
                    is_trivial = True
                    break
            if not is_trivial:
                worthy.append(content)
        return worthy

    async def extract(self, texts: List[str]) -> List[MemoryFact]:
        """Extrae facts estructurados via Ollama."""
        if not texts:
            return []

        dialogue = "\n".join(texts[-10:])[:4000]

        existing = self._list_existing()
        existing_desc = "\n".join(
            f"- {m['topic_key']}: {m['fact'][:80]}" for m in existing
        ) if existing else "(none)"

        prompt = (
            "Extract user preferences, constraints, or project facts from this dialogue.\n"
            "Return a JSON array. Each item: {fact, category, confidence, source, topic_key, entity_links}.\n"
            "- fact: the key information to remember (1-2 sentences)\n"
            "- category: one of 'user', 'feedback', 'project', 'reference', 'decision', 'bug'\n"
            "- confidence: 0.0-1.0 how certain this is a fact worth remembering\n"
            "- source: brief context (e.g. 'user preference', 'project requirement')\n"
            "- topic_key: kebab-case identifier for this topic (e.g. 'user-preference-tabs')\n"
            "- entity_links: list of entity names mentioned (e.g. ['director', 'cerebro.db'])\n"
            "If nothing new or already covered, return [].\n\n"
            f"Existing memories:\n{existing_desc}\n\n"
            f"Dialogue:\n{dialogue}"
        )

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.ollama_url}/api/generate",
                    json={
                        "model": self.extraction_model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.2, "num_predict": 1000},
                    },
                )
                if resp.status_code == 200:
                    text = resp.json().get("response", "").strip()
                    # Quitar wrappers de markdown
                    text = __import__("re").sub(r'```json\s*|\s*```', '', text)
                    match = __import__("re").search(r'\[.*?\]', text, __import__("re").DOTALL)
                    if not match:
                        match = __import__("re").search(r'\{.*\}', text, __import__("re").DOTALL)
                    if match:
                        raw = match.group()
                        raw = raw.replace("'", '"')
                        raw = raw.replace(",\n", "\n")
                        raw = raw.replace(",}", "}")
                        raw = raw.replace(",]", "]")
                        raw = __import__("re").sub(r'\}\s*\{', '},{', raw)
                        try:
                            items = json.loads(raw)
                        except json.JSONDecodeError:
                            return []
                        if isinstance(items, dict):
                            items = [items]
                        facts = []
                        for item in items:
                            if isinstance(item, str):
                                continue
                            if isinstance(item, dict) and item.get("fact") and item.get("topic_key"):
                                facts.append(MemoryFact(
                                    fact=item["fact"],
                                    category=item.get("category", "general"),
                                    confidence=item.get("confidence", 0.5),
                                    source=item.get("source", ""),
                                    topic_key=item.get("topic_key", ""),
                                    entity_links=item.get("entity_links", []),
                                ))
                        return facts
        except Exception as e:
            logger.warning(f"Memory extraction failed: {e}")

        return []

    async def consolidate(self, new_facts: List[MemoryFact]):
        """
        Two-Step Memory Save:
        1. Write to staging table (atomic)
        2. Move to main table only if staging write succeeded
        
        ADD-only: Inserta nuevos facts. Si topic_key existe, actualiza (upsert).
        Resolución de conflictos en retrieval, no aquí.
        """
        if not new_facts:
            return

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        # Two-Step Save: Stage first
        staged_ids = []
        try:
            c.execute("""CREATE TABLE IF NOT EXISTS memories_staging (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                confidence REAL DEFAULT 0.5,
                source TEXT DEFAULT '',
                timestamp REAL DEFAULT (strftime('%s', 'now')),
                topic_key TEXT DEFAULT '',
                entity_links TEXT DEFAULT '[]'
            )""")
            conn.commit()

            for fact in new_facts:
                c.execute("""INSERT INTO memories_staging 
                    (fact, category, confidence, source, timestamp, topic_key, entity_links)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", (
                    fact.fact, fact.category, fact.confidence, fact.source,
                    fact.timestamp, fact.topic_key, json.dumps(fact.entity_links)
                ))
                staged_ids.append(c.lastrowid)

            conn.commit()  # Step 1 complete: staging write succeeded

            # Step 2: Move from staging to main table
            for sid in staged_ids:
                row = c.execute("SELECT * FROM memories_staging WHERE id = ?", (sid,)).fetchone()
                if not row:
                    continue

                topic_key = row[6]
                fact_text = row[1]
                norm_hash = self._compute_hash(fact_text, topic_key)
                now = time.time()

                existing = c.execute(
                    "SELECT id, revision_count, fact FROM memories WHERE topic_key = ? AND is_active = 1 AND deleted_at IS NULL",
                    (topic_key,),
                ).fetchone()

                if existing:
                    c.execute("""UPDATE memories SET 
                        fact = ?, category = ?, confidence = ?, source = ?, 
                        timestamp = ?, entity_links = ?, revision_count = revision_count + 1,
                        last_seen_at = ?
                        WHERE id = ?""", (
                        fact_text, row[2], row[3], row[4],
                        now, row[7], now, existing[0]
                    ))

                else:
                    dup = c.execute(
                        "SELECT id FROM memories WHERE normalized_hash = ? AND deleted_at IS NULL AND id != ?",
                        (norm_hash, 0),
                    ).fetchone()
                    if dup:
                        c.execute("UPDATE memories SET duplicate_count = duplicate_count + 1, last_seen_at = ? WHERE id = ?",
                                  (now, dup[0]))
                    else:
                        c.execute("""INSERT INTO memories 
                            (fact, category, confidence, source, timestamp, topic_key, entity_links, normalized_hash)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (
                            fact_text, row[2], row[3], row[4], now, topic_key, row[7], norm_hash
                        ))                        
                        new_id = c.lastrowid
                        self._detect_and_record_conflicts(c, new_id, fact_text, topic_key)

            conn.commit()  # Step 2 complete: main table write succeeded

            # Clean staging
            for sid in staged_ids:
                c.execute("DELETE FROM memories_staging WHERE id = ?", (sid,))
            conn.commit()

        except Exception as e:
            logger.error(f"Two-step memory save failed: {e}")
            conn.rollback()
            # Try to clean staging on failure
            for sid in staged_ids:
                try:
                    c.execute("DELETE FROM memories_staging WHERE id = ?", (sid,))
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()

        self._rebuild_fts()

        # Entity linking: auto-discover relationships from new facts
        self.auto_link_entities_from_facts(new_facts)

        if self._count_memories() > self.max_memories:
            self._prune_old_memories()

    async def run_pipeline(self, messages: List[Dict]):
        """Ejecuta las 3 fases en secuencia."""
        worthy = await self.select(messages)
        if not worthy:
            return {"status": "skipped", "reason": "no worthy messages"}

        facts = await self.extract(worthy)
        if not facts:
            return {"status": "skipped", "reason": "no facts extracted"}

        await self.consolidate(facts)

        return {
            "status": "success",
            "facts_extracted": len(facts),
            "facts": [{"fact": f.fact, "topic_key": f.topic_key} for f in facts],
        }

    def search(self, query: str, limit: int = 10) -> List[Dict]:
        """Búsqueda FTS5 híbrida (BM25 + temporal boost)."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("""SELECT m.*, 
            bm25(memories_fts) as rank
            FROM memories m
            JOIN memories_fts ON m.id = memories_fts.rowid
            WHERE memories_fts MATCH ?
            AND m.is_active = 1
            ORDER BY rank, m.timestamp DESC
            LIMIT ?""", (query, limit))

        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

    def get_by_topic(self, topic_key: str) -> Optional[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM memories WHERE topic_key = ? AND is_active = 1 ORDER BY timestamp DESC LIMIT 1", (topic_key,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

    def _detect_and_record_conflicts(self, c, new_id: int, fact: str, topic_key: str):
        try:
            candidates = c.execute(
                "SELECT id, fact, topic_key FROM memories WHERE id != ? AND is_active = 1 AND deleted_at IS NULL ORDER BY timestamp DESC LIMIT 20",
                (new_id,),
            ).fetchall()
            for row in candidates:
                existing_fact = row[1].lower().strip()
                new_fact = fact.lower().strip()
                if topic_key and row[2] == topic_key and existing_fact != new_fact:
                    try:
                        c.execute("INSERT OR IGNORE INTO memory_relations (source_id, target_id, relation_type, reason) VALUES (?, ?, 'supersedes', ?)",
                                  (new_id, row[0], f"Updated version of topic '{topic_key}'"))
                    except Exception:
                        pass
                    continue
                common = len(set(existing_fact.split()) & set(new_fact.split()))
                total = max(len(set(existing_fact.split()) | set(new_fact.split())), 1)
                overlap = common / total
                if 0.4 < overlap < 0.85:
                    try:
                        c.execute("INSERT OR IGNORE INTO memory_relations (source_id, target_id, relation_type, reason) VALUES (?, ?, 'conflicts_with', ?)",
                                  (new_id, row[0], f"Semantic overlap {overlap:.0%}"))
                    except Exception:
                        pass
                elif overlap >= 0.85:
                    try:
                        c.execute("INSERT OR IGNORE INTO memory_relations (source_id, target_id, relation_type, reason) VALUES (?, ?, 'related', ?)",
                                  (new_id, row[0], f"High overlap {overlap:.0%}"))
                    except Exception:
                        pass
        except Exception:
            pass

    def get_conflicts(self, memory_id: int = None) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if memory_id:
            c.execute("""SELECT mr.*, m1.fact as source_fact, m2.fact as target_fact
                FROM memory_relations mr
                JOIN memories m1 ON mr.source_id = m1.id
                JOIN memories m2 ON mr.target_id = m2.id
                WHERE (mr.source_id = ? OR mr.target_id = ?) AND mr.relation_type IN ('conflicts_with', 'supersedes')
                ORDER BY mr.created_at DESC""", (memory_id, memory_id))
        else:
            c.execute("""SELECT mr.*, m1.fact as source_fact, m2.fact as target_fact
                FROM memory_relations mr
                JOIN memories m1 ON mr.source_id = m1.id
                JOIN memories m2 ON mr.target_id = m2.id
                WHERE mr.relation_type IN ('conflicts_with', 'supersedes')
                ORDER BY mr.created_at DESC LIMIT 50""")
        results = [dict(r) for r in c.fetchall()]
        conn.close()
        return results

    def soft_delete(self, memory_id: int) -> bool:
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("UPDATE memories SET deleted_at = strftime('%s', 'now'), is_active = 0 WHERE id = ?", (memory_id,))
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False

    def _list_existing(self) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT topic_key, fact FROM memories WHERE is_active = 1 ORDER BY timestamp DESC LIMIT 50")
        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

    def _count_memories(self) -> int:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM memories WHERE is_active = 1")
        count = c.fetchone()[0]
        conn.close()
        return count

    def _rebuild_fts(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        conn.commit()
        conn.close()

    def _prune_old_memories(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""UPDATE memories SET is_active = 0 
            WHERE id NOT IN (
                SELECT id FROM memories WHERE is_active = 1 
                ORDER BY timestamp DESC LIMIT ?
            )""", (self.max_memories,))
        conn.commit()
        conn.close()
        logger.info(f"Memory pruned to {self.max_memories} active memories")

    # ─── Claude Memory Tool Interface ─────────────────────────────────────

    def create_memory(self, fact: str, category: str = "general", 
                      topic_key: str = "", source: str = "", 
                      entity_links: List[str] = None) -> Dict:
        """
        Claude Memory Tool: create_memory
        Crea una nueva memoria persistente. Similar a Claude's memory tool.
        """
        if not topic_key:
            topic_key = fact[:50].lower().replace(" ", "-").replace("/", "-")
        
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        existing = c.execute(
            "SELECT id FROM memories WHERE topic_key = ? AND is_active = 1",
            (topic_key,),
        ).fetchone()
        
        if existing:
            c.execute("""UPDATE memories SET 
                fact = ?, category = ?, source = ?, 
                timestamp = ?, entity_links = ?
                WHERE id = ?""", (
                fact, category, source, time.time(),
                json.dumps(entity_links or []), existing[0]
            ))
            action = "updated"
            memory_id = existing[0]
        else:
            c.execute("""INSERT INTO memories 
                (fact, category, confidence, source, timestamp, topic_key, entity_links)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (
                fact, category, 0.8, source, time.time(), topic_key,
                json.dumps(entity_links or [])
            ))
            memory_id = c.lastrowid
            action = "created"
        
        conn.commit()
        conn.close()
        self._rebuild_fts()
        
        return {
            "id": memory_id,
            "action": action,
            "topic_key": topic_key,
            "fact": fact[:100],
        }

    def read_memory(self, topic_key: str) -> Optional[Dict]:
        """
        Claude Memory Tool: read_memory
        Lee una memoria específica por topic_key.
        """
        return self.get_by_topic(topic_key)

    def update_memory(self, topic_key: str, fact: str, 
                      category: str = None, source: str = None) -> Dict:
        """
        Claude Memory Tool: update_memory
        Actualiza una memoria existente.
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        existing = c.execute(
            "SELECT id, fact, category, source FROM memories WHERE topic_key = ? AND is_active = 1",
            (topic_key,),
        ).fetchone()
        
        if not existing:
            conn.close()
            return {"error": f"Memory not found: {topic_key}"}
        
        new_fact = fact or existing[1]
        new_category = category or existing[2]
        new_source = source or existing[3]
        
        c.execute("""UPDATE memories SET 
            fact = ?, category = ?, source = ?, timestamp = ?
            WHERE id = ?""", (new_fact, new_category, new_source, time.time(), existing[0]))
        
        conn.commit()
        conn.close()
        self._rebuild_fts()
        
        return {
            "id": existing[0],
            "topic_key": topic_key,
            "fact": new_fact[:100],
            "action": "updated",
        }

    def delete_memory(self, topic_key: str) -> Dict:
        """
        Claude Memory Tool: delete_memory
        Elimina una memoria (soft delete).
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute("UPDATE memories SET is_active = 0 WHERE topic_key = ?", (topic_key,))
        affected = c.rowcount
        
        conn.commit()
        conn.close()
        
        return {
            "topic_key": topic_key,
            "deleted": affected > 0,
            "action": "soft_deleted",
        }

    def get_session_context(self, project: str = "default", limit: int = 10) -> List[Dict]:
        """
        Claude Memory Tool: get_session_context
        Recupera contexto de sesión anterior para reanudar trabajo.
        Similar a Claude's session initialization pattern.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        c.execute("""SELECT * FROM memories 
            WHERE is_active = 1 AND (category = 'project' OR category = 'decision')
            ORDER BY timestamp DESC LIMIT ?""", (limit,))
        
        results = [dict(row) for row in c.fetchall()]
        conn.close()
        
        return results

    def get_progressive_disclosure(self, query: str, layer: str = "search") -> Dict:
        """
        Progressive Disclosure Pattern (3 capas):
        - layer="search": Resultados compactos (título + snippet)
        - layer="timeline": Contexto cronológico
        - layer="full": Contenido completo
        """
        if layer == "search":
            results = self.search(query, limit=5)
            return {
                "layer": "search",
                "count": len(results),
                "results": [
                    {"id": r["id"], "topic_key": r["topic_key"], 
                     "fact": r["fact"][:100], "category": r["category"]}
                    for r in results
                ],
            }
        elif layer == "timeline":
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""SELECT * FROM memories 
                WHERE is_active = 1 ORDER BY timestamp DESC LIMIT 20""")
            results = [dict(row) for row in c.fetchall()]
            conn.close()
            return {
                "layer": "timeline",
                "count": len(results),
                "results": results,
            }
        elif layer == "full":
            results = self.search(query, limit=3)
            return {
                "layer": "full",
                "count": len(results),
                "results": results,
            }
        
        return {"error": f"Unknown layer: {layer}"}

    # ─── Entity Linking (mem0 V3 pattern) ─────────────────────────────────────

    def extract_entities(self, text: str) -> List[Tuple[str, str]]:
        """
        Extract entities from text using pattern matching.
        Returns list of (entity_name, entity_type) tuples.
        
        Entity types: 'person', 'project', 'technology', 'file', 'concept', 'organization'
        """
        entities = []
        seen = set()

        # Technology patterns
        tech_patterns = [
            (r'\b(Python|JavaScript|TypeScript|Rust|Go|Java|C\+\+|C#|Ruby|Swift|Kotlin)\b', 'technology'),
            (r'\b(React|Vue|Angular|Svelte|Next\.js|Nuxt|Django|FastAPI|Flask|Express)\b', 'technology'),
            (r'\b(Docker|Kubernetes|Terraform|Ansible|Git|Linux|Windows|macOS)\b', 'technology'),
            (r'\b(Ollama|OpenAI|Anthropic|Google|Microsoft|AWS|Azure|GCP)\b', 'technology'),
            (r'\b(SQLite|PostgreSQL|MySQL|MongoDB|Redis|FAISS|Chroma)\b', 'technology'),
        ]

        # File patterns
        file_patterns = [
            (r'[\w./\\-]+\.(py|js|ts|tsx|jsx|json|yaml|yml|toml|md|txt|cfg|conf|ini|sh|bat|ps1)', 'file'),
        ]

        # Project/Code patterns (camelCase, snake_case identifiers)
        code_patterns = [
            (r'\b([A-Z][a-zA-Z]+(?:Nexus|Manager|Controller|Handler|Engine|Server|Client|Bridge|Gem|Agent|Pipeline|Registry|Monitor|Guard|Daemon|Optimizer|Curator|Trainer|Collector|Gateway|Router))\b', 'concept'),
        ]

        all_patterns = tech_patterns + file_patterns + code_patterns

        for pattern, entity_type in all_patterns:
            for match in re.finditer(pattern, text):
                name = match.group(1) if match.lastindex else match.group(0)
                if name.lower() not in seen and len(name) > 2:
                    entities.append((name, entity_type))
                    seen.add(name.lower())

        return entities

    def register_entity(self, name: str, entity_type: str = 'concept') -> int:
        """Register or update an entity in the registry."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("""INSERT INTO entities (name, entity_type, mention_count, first_seen, last_seen)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                mention_count = mention_count + 1,
                last_seen = ?""", (name, entity_type, time.time(), time.time(), time.time()))

        entity_id = c.lastrowid
        conn.commit()
        conn.close()
        return entity_id

    def link_entities(self, entities: List[str], relation_type: str = 'co_occurrence',
                      evidence: str = '', weight: float = 1.0):
        """Create relationships between co-occurring entities."""
        if len(entities) < 2:
            return

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        # Ensure all entities exist
        for entity_name in entities:
            c.execute("""INSERT OR IGNORE INTO entities (name, entity_type) VALUES (?, ?)""",
                      (entity_name, 'concept'))

        # Create pairwise relations
        for i, src in enumerate(entities):
            for tgt in entities[i + 1:]:
                try:
                    c.execute("""INSERT INTO entity_relations 
                        (source_entity, target_entity, relation_type, weight, evidence)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(source_entity, target_entity, relation_type) DO UPDATE SET
                            weight = weight + ?,
                            evidence = ?""",
                        (src, tgt, relation_type, weight, evidence, weight, evidence[:200]))
                except sqlite3.IntegrityError:
                    pass

        conn.commit()
        conn.close()

    def map_entity_to_memory(self, entity_name: str, memory_id: int, relevance: float = 1.0):
        """Link an entity to a specific memory."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("SELECT id FROM entities WHERE name = ?", (entity_name,))
        row = c.fetchone()
        if row:
            c.execute("""INSERT OR REPLACE INTO entity_memory_map (entity_id, memory_id, relevance)
                VALUES (?, ?, ?)""", (row[0], memory_id, relevance))

        conn.commit()
        conn.close()

    def get_entity_graph(self, min_weight: float = 1.0, limit: int = 100) -> Dict:
        """Get the entity relationship graph."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("""SELECT source_entity, target_entity, relation_type, weight, evidence
            FROM entity_relations WHERE weight >= ?
            ORDER BY weight DESC LIMIT ?""", (min_weight, limit))

        edges = [dict(row) for row in c.fetchall()]

        c.execute("""SELECT name, entity_type, mention_count, first_seen, last_seen
            FROM entities ORDER BY mention_count DESC LIMIT 50""")
        nodes = [dict(row) for row in c.fetchall()]

        conn.close()

        return {
            "nodes": nodes,
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
        }

    def search_by_entity(self, entity_name: str, limit: int = 10) -> List[Dict]:
        """Search memories connected to a specific entity."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("""SELECT m.*, e.name as entity_name, em.relevance
            FROM memories m
            JOIN entity_memory_map em ON m.id = em.memory_id
            JOIN entities e ON em.entity_id = e.id
            WHERE e.name = ? AND m.is_active = 1
            ORDER BY em.relevance DESC, m.timestamp DESC
            LIMIT ?""", (entity_name, limit))

        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

    def get_related_entities(self, entity_name: str, min_weight: float = 1.0, limit: int = 20) -> List[Dict]:
        """Get entities related to a given entity."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("""SELECT 
            CASE WHEN er.source_entity = ? THEN er.target_entity ELSE er.source_entity END as related_name,
            e.entity_type,
            er.relation_type,
            er.weight,
            er.evidence
            FROM entity_relations er
            JOIN entities e ON (
                CASE WHEN er.source_entity = ? THEN er.target_entity ELSE er.source_entity END
            ) = e.name
            WHERE (er.source_entity = ? OR er.target_entity = ?) AND er.weight >= ?
            ORDER BY er.weight DESC LIMIT ?""",
            (entity_name, entity_name, entity_name, entity_name, min_weight, limit))

        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

    def entity_aware_search(self, query: str, limit: int = 10) -> List[Dict]:
        """
        Entity-aware search: combines FTS5 with entity graph boosting.
        Results connected to popular entities get boosted.
        """
        # Step 1: FTS5 search
        fts_results = self.search(query, limit=limit * 2)

        if not fts_results:
            return []

        # Step 2: Extract entities from query
        query_entities = self.extract_entities(query)
        query_entity_names = [name for name, _ in query_entities]

        # Step 3: Boost results connected to query entities
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        boosted = []
        for result in fts_results:
            boost = 0.0

            # Check if result has entity links
            entity_links = json.loads(result.get('entity_links', '[]'))

            # Direct entity match
            for qe in query_entity_names:
                if qe.lower() in [el.lower() for el in entity_links]:
                    boost += 2.0

            # Related entity match (1-hop)
            for qe in query_entity_names:
                c.execute("""SELECT COUNT(*) FROM entity_relations
                    WHERE (source_entity = ? OR target_entity = ?) AND weight >= 1""",
                    (qe, qe))
                related_count = c.fetchone()[0]
                if related_count > 0:
                    boost += 0.5 * min(related_count, 3)

            result['entity_boost'] = boost
            result['score'] = result.get('rank', 0) - boost
            boosted.append(result)

        conn.close()

        # Sort by boosted score
        boosted.sort(key=lambda x: x['score'])
        return boosted[:limit]

    def auto_link_entities_from_facts(self, facts: List[MemoryFact]):
        """Auto-discover and link entities from new facts."""
        for fact in facts:
            # Extract entities from fact text
            entities = self.extract_entities(fact.fact)

            if entities:
                entity_names = []
                for name, etype in entities:
                    eid = self.register_entity(name, etype)
                    entity_names.append(name)

                # Link co-occurring entities
                if len(entity_names) >= 2:
                    self.link_entities(entity_names, 'co_occurrence',
                                       evidence=fact.fact[:200], weight=fact.confidence)

                # Map entities to the memory
                if fact.topic_key:
                    conn = sqlite3.connect(self.db_path)
                    c = conn.cursor()
                    c.execute("SELECT id FROM memories WHERE topic_key = ?", (fact.topic_key,))
                    row = c.fetchone()
                    if row:
                        for name in entity_names:
                            self.map_entity_to_memory(name, row[0], fact.confidence)
                    conn.close()

    # ─── Graph Memory Enhancements ──────────────────────────────────────

    def find_path_between_entities(self, source: str, target: str, max_hops: int = 4) -> List[Dict]:
        """BFS shortest path between two entities through the entity graph."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        visited = {source}
        queue = [(source, [source])]
        while queue and len(visited) < 100:
            current, path = queue.pop(0)
            if len(path) > max_hops:
                continue
            c.execute("""SELECT source_entity, target_entity FROM entity_relations
                WHERE (source_entity = ? OR target_entity = ?) AND weight >= 0.5""",
                (current, current))
            for row in c.fetchall():
                neighbor = row[1] if row[0] == current else row[0]
                if neighbor == target:
                    conn.close()
                    return [{"path": path + [neighbor], "hops": len(path)}]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))
        conn.close()
        return []

    def get_memory_neighborhood(self, memory_id: int, depth: int = 1) -> Dict:
        """Get memories connected to a given memory via shared entities."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""SELECT e.name FROM entities e
            JOIN entity_memory_map em ON e.id = em.entity_id
            WHERE em.memory_id = ?""", (memory_id,))
        entities = [r["name"] for r in c.fetchall()]
        if not entities:
            conn.close()
            return {"memory_id": memory_id, "entities": [], "neighbors": []}
        c.execute("""SELECT DISTINCT m.id, m.fact, m.topic_key, m.category, em.relevance
            FROM memories m
            JOIN entity_memory_map em ON m.id = em.memory_id
            JOIN entities e ON em.entity_id = e.id
            WHERE e.name IN ({}) AND m.id != ? AND m.is_active = 1
            ORDER BY em.relevance DESC LIMIT 20""".format(
                ','.join('?' * len(entities))),
            list(entities) + [memory_id])
        neighbors = [dict(r) for r in c.fetchall()]
        conn.close()
        return {"memory_id": memory_id, "entities": entities, "neighbors": neighbors}

    def search_by_topic_graph(self, query: str, limit: int = 10) -> List[Dict]:
        """Search using topic_key similarity + entity graph expansion."""
        results = self.search(query, limit=limit)
        if len(results) >= limit:
            return results[:limit]
        topic_keys = [r["topic_key"] for r in results if r.get("topic_key")]
        if not topic_keys:
            return results
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        placeholders = ','.join('?' * len(topic_keys))
        c.execute("""SELECT DISTINCT m2.* FROM memories m1
            JOIN entity_memory_map em1 ON m1.id = em1.memory_id
            JOIN entity_memory_map em2 ON em1.entity_id = em2.entity_id
            JOIN memories m2 ON em2.memory_id = m2.id
            WHERE m1.topic_key IN ({}) AND m2.id != m1.id
            AND m2.is_active = 1
            LIMIT ?""".format(placeholders), topic_keys + [limit - len(results)])
        seen = {r["id"] for r in results}
        for row in c.fetchall():
            if row["id"] not in seen:
                results.append(dict(row))
                seen.add(row["id"])
        conn.close()
        return results[:limit]

    def get_entity_clusters(self, min_size: int = 2) -> List[Dict]:
        """Find entity clusters (groups of densely connected entities)."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""SELECT source_entity, target_entity, weight
            FROM entity_relations WHERE weight >= 1.0
            ORDER BY weight DESC LIMIT 200""")
        edges = [(r[0], r[1], r[2]) for r in c.fetchall()]
        conn.close()
        clusters = []
        assigned = set()
        for src, tgt, w in edges:
            found = False
            for cluster in clusters:
                cluster_names = {e["name"] for e in cluster["entities"]}
                if src in cluster_names or tgt in cluster_names:
                    if src not in cluster_names:
                        cluster["entities"].append({"name": src, "weight": w})
                    if tgt not in cluster_names:
                        cluster["entities"].append({"name": tgt, "weight": w})
                    cluster["weight"] += w
                    found = True
                    break
            if not found:
                clusters.append({
                    "entities": [{"name": src, "weight": w}, {"name": tgt, "weight": w}],
                    "weight": w,
                    "size": 2,
                })
            assigned.add(src)
            assigned.add(tgt)
        return [c for c in clusters if c["size"] >= min_size]

    def get_stats(self) -> Dict:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM memories WHERE is_active = 1")
        active = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM memories")
        total = c.fetchone()[0]
        c.execute("SELECT DISTINCT category FROM memories WHERE is_active = 1")
        categories = [r[0] for r in c.fetchall()]

        # Entity stats
        c.execute("SELECT COUNT(*) FROM entities")
        entity_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM entity_relations")
        relation_count = c.fetchone()[0]
        c.execute("SELECT name, mention_count FROM entities ORDER BY mention_count DESC LIMIT 5")
        top_entities = [{"name": r[0], "mentions": r[1]} for r in c.fetchall()]

        conn.close()
        return {
            "active_memories": active,
            "total_memories": total,
            "categories": categories,
            "db_path": self.db_path,
            "entity_count": entity_count,
            "entity_relations": relation_count,
            "top_entities": top_entities,
        }

    # --- Session Summary at Shutdown ---

    def generate_session_summary(self, session_messages: List[Dict]) -> str:
        """
        Generate a concise session summary for persistence at shutdown.
        Captures: goals, decisions, files changed, remaining work.
        """
        if not session_messages:
            return "No session activity to summarize."

        # Extract key information
        goals = []
        decisions = []
        files_changed = []
        errors = []

        for msg in session_messages:
            content = msg.get("content", "")
            role = msg.get("role", "")

            if role == "user" and isinstance(content, str):
                if any(kw in content.lower() for kw in ["goal", "task", "objective", "need to"]):
                    goals.append(content[:200])
                if any(kw in content.lower() for kw in ["file", "edit", "modify", "create"]):
                    file_match = __import__("re").search(r'[\w/\\]+\.\w+', content)
                    if file_match:
                        files_changed.append(file_match.group())

            if role == "assistant" and isinstance(content, str):
                if any(kw in content.lower() for kw in ["decided", "chose", "will use", "approach"]):
                    decisions.append(content[:200])
                if any(kw in content.lower() for kw in ["error", "failed", "exception"]):
                    errors.append(content[:200])

        summary_parts = ["Session Summary:"]
        if goals:
            summary_parts.append(f"Goals: {'; '.join(goals[:3])}")
        if decisions:
            summary_parts.append(f"Decisions: {'; '.join(decisions[:3])}")
        if files_changed:
            summary_parts.append(f"Files: {', '.join(set(files_changed))}")
        if errors:
            summary_parts.append(f"Errors: {len(errors)} encountered")
        summary_parts.append(f"Total messages: {len(session_messages)}")

        return "\n".join(summary_parts)

    def save_session_summary(self, session_messages: List[Dict], session_id: str = "") -> Dict:
        """
        Save session summary to memory at shutdown.
        This ensures session context persists across restarts.
        """
        summary = self.generate_session_summary(session_messages)
        session_id = session_id or f"session-{int(time.time())}"

        # Save as a special session memory
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("""INSERT INTO memories 
            (fact, category, confidence, source, timestamp, topic_key, entity_links)
            VALUES (?, ?, ?, ?, ?, ?, ?)""", (
            summary,
            "session_summary",
            0.9,
            "auto_shutdown",
            time.time(),
            f"session-{session_id}",
            json.dumps([])
        ))

        memory_id = c.lastrowid
        conn.commit()
        conn.close()
        self._rebuild_fts()

        logger.info(f"Session summary saved: {session_id}")
        return {
            "id": memory_id,
            "session_id": session_id,
            "summary": summary[:200],
        }

    def load_last_session_summary(self) -> Optional[Dict]:
        """Load the most recent session summary for context recovery."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("""SELECT * FROM memories 
            WHERE category = 'session_summary' 
            AND is_active = 1 
            ORDER BY timestamp DESC LIMIT 1""")

        row = c.fetchone()
        conn.close()

        return dict(row) if row else None
