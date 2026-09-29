"""
FTS5 Search - Busqueda rapida indexada sobre sesiones y trazas para SuperNEXUS v2

Usa SQLite FTS5 para indexar:
- Historial de sesiones
- Trazas de ejecucion
- Engramas cognitivos
- Observaciones del Knowledge Vault

Patrones:
- FTS5 con contentless tables para eficiencia
- Triggers para mantener sincronizacion automatica
- Busqueda por relevancia (BM25)
- Highlighting de resultados
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("nexus-fts5")


@dataclass
class SearchResult:
    """Resultado de busqueda FTS5"""
    doc_id: int
    table: str
    content: str
    snippet: str = ""
    rank: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class FTS5Search:
    """
    Motor de busqueda FTS5 para SuperNEXUS.

    Uso:
        fts = FTS5Search()
        fts.index_session("session_1", "user: refactor code\nassistant: done")
        results = fts.search("refactor code")
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path.home() / ".nexus" / "brain" / "search.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._get_conn()
        c = conn.cursor()

        # Tabla principal de documentos indexados
        c.execute("""CREATE TABLE IF NOT EXISTS search_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT NOT NULL,
            table_name TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")

        # FTS5 virtual table
        try:
            c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                content,
                content='search_docs',
                content_rowid='id'
            )""")
        except sqlite3.OperationalError:
            logger.warning("FTS5 not available, falling back to LIKE search")

        # Triggers para sincronizacion automatica
        c.execute("""CREATE TRIGGER IF NOT EXISTS search_docs_ai AFTER INSERT ON search_docs BEGIN
            INSERT INTO search_fts(rowid, content) VALUES (new.id, new.content);
        END""")

        c.execute("""CREATE TRIGGER IF NOT EXISTS search_docs_ad AFTER DELETE ON search_docs BEGIN
            INSERT INTO search_fts(search_fts, rowid, content) VALUES ('delete', old.id, old.content);
        END""")

        c.execute("""CREATE TRIGGER IF NOT EXISTS search_docs_au AFTER UPDATE ON search_docs BEGIN
            INSERT INTO search_fts(search_fts, rowid, content) VALUES ('delete', old.id, old.content);
            INSERT INTO search_fts(rowid, content) VALUES (new.id, new.content);
        END""")

        # Indices para busquedas secundarias
        c.execute("CREATE INDEX IF NOT EXISTS idx_search_docs_table ON search_docs(table_name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_search_docs_doc_id ON search_docs(doc_id)")

        conn.commit()
        conn.close()

    def index_document(self, doc_id: str, table_name: str, content: str, metadata: Dict = None):
        """Indexa un documento"""
        conn = self._get_conn()
        c = conn.cursor()
        now = datetime.now().isoformat()
        try:
            c.execute("SELECT id FROM search_docs WHERE doc_id = ? AND table_name = ?", (doc_id, table_name))
            existing = c.fetchone()

            if existing:
                c.execute("""UPDATE search_docs SET content = ?, metadata = ?, updated_at = ?
                    WHERE doc_id = ? AND table_name = ?""", (
                    content,
                    str(metadata or {}),
                    now,
                    doc_id,
                    table_name,
                ))
            else:
                c.execute("""INSERT INTO search_docs (doc_id, table_name, content, metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)""", (
                    doc_id,
                    table_name,
                    content,
                    str(metadata or {}),
                    now,
                    now,
                ))
            conn.commit()
        except Exception as e:
            logger.error(f"Error indexing document {doc_id}: {e}")
        finally:
            conn.close()

    def index_session(self, session_id: str, content: str, metadata: Dict = None):
        """Indexa una sesion completa"""
        self.index_document(session_id, "sessions", content, metadata)

    def index_trace(self, trace_id: str, content: str, metadata: Dict = None):
        """Indexa una traza de ejecucion"""
        self.index_document(trace_id, "traces", content, metadata)

    def index_cognitive_memory(self, memory_id: str, content: str, metadata: Dict = None):
        """Indexa una memoria cognitiva"""
        self.index_document(memory_id, "cognitive_memories", content, metadata)

    def index_observation(self, obs_id: str, content: str, metadata: Dict = None):
        """Indexa una observacion"""
        self.index_document(obs_id, "observations", content, metadata)

    def search(self, query: str, table_filter: str = None, limit: int = 20) -> List[SearchResult]:
        """
        Busqueda FTS5 con opcion de filtrar por tabla.

        Soporta sintaxis FTS5:
        - "refactor code" → busqueda de frases
        - "refactor OR code" → busqueda booleana
        - "refactor NEAR code" → proximidad
        """
        conn = self._get_conn()
        c = conn.cursor()
        results = []

        try:
            if table_filter:
                c.execute("""SELECT sd.id, sd.doc_id, sd.table_name, sd.content, sd.metadata,
                    search_fts.rank
                    FROM search_fts
                    JOIN search_docs sd ON sd.id = search_fts.rowid
                    WHERE search_fts MATCH ? AND sd.table_name = ?
                    ORDER BY search_fts.rank LIMIT ?""", (query, table_filter, limit))
            else:
                c.execute("""SELECT sd.id, sd.doc_id, sd.table_name, sd.content, sd.metadata,
                    search_fts.rank
                    FROM search_fts
                    JOIN search_docs sd ON sd.id = search_fts.rowid
                    WHERE search_fts MATCH ?
                    ORDER BY search_fts.rank LIMIT ?""", (query, limit))

            for row in c.fetchall():
                # Generar snippet
                snippet = self._generate_snippet(row["content"], query)
                results.append(SearchResult(
                    doc_id=row["doc_id"],
                    table=row["table_name"],
                    content=row["content"][:500],
                    snippet=snippet,
                    rank=row["rank"] if row["rank"] else 0.0,
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                ))

        except sqlite3.OperationalError as e:
            logger.warning(f"FTS5 search failed, falling back to LIKE: {e}")
            results = self._fallback_search(query, table_filter, limit, conn)

        conn.close()
        return results

    def _fallback_search(self, query: str, table_filter: str, limit: int, conn) -> List[SearchResult]:
        """Busqueda fallback usando LIKE cuando FTS5 no esta disponible"""
        c = conn.cursor()
        results = []

        where = "content LIKE ?"
        params = [f"%{query}%"]
        if table_filter:
            where += " AND table_name = ?"
            params.append(table_filter)

        c.execute(f"SELECT id, doc_id, table_name, content, metadata FROM search_docs WHERE {where} LIMIT ?",
                  params + [limit])

        for row in c.fetchall():
            results.append(SearchResult(
                doc_id=row["doc_id"],
                table=row["table_name"],
                content=row["content"][:500],
                snippet=self._generate_snippet(row["content"], query),
                rank=1.0,
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            ))

        return results

    def _generate_snippet(self, content: str, query: str, context: int = 50) -> str:
        """Genera snippet con contexto alrededor de la busqueda"""
        content_lower = content.lower()
        query_lower = query.lower()

        # Buscar primera coincidencia
        pos = content_lower.find(query_lower)
        if pos == -1:
            return content[:100] + "..." if len(content) > 100 else content

        start = max(0, pos - context)
        end = min(len(content), pos + len(query) + context)

        snippet = content[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet += "..."

        return snippet

    def delete_document(self, doc_id: str, table_name: str) -> bool:
        """Elimina un documento del indice"""
        conn = self._get_conn()
        c = conn.cursor()
        try:
            c.execute("DELETE FROM search_docs WHERE doc_id = ? AND table_name = ?", (doc_id, table_name))
            conn.commit()
            return c.rowcount > 0
        finally:
            conn.close()

    def get_stats(self) -> Dict:
        """Obtiene estadisticas del indice"""
        conn = self._get_conn()
        c = conn.cursor()
        try:
            c.execute("SELECT COUNT(*) FROM search_docs")
            total = c.fetchone()[0]

            c.execute("SELECT table_name, COUNT(*) FROM search_docs GROUP BY table_name")
            by_table = {row[0]: row[1] for row in c.fetchall()}

            return {
                "total_documents": total,
                "by_table": by_table,
                "fts5_enabled": True,
            }
        finally:
            conn.close()

    def rebuild_index(self):
        """Reconstruye el indice FTS5 completo"""
        conn = self._get_conn()
        c = conn.cursor()
        try:
            c.execute("INSERT INTO search_fts(search_fts) VALUES ('rebuild')")
            conn.commit()
            logger.info("FTS5 index rebuilt")
        except Exception as e:
            logger.error(f"Error rebuilding FTS5 index: {e}")
        finally:
            conn.close()
