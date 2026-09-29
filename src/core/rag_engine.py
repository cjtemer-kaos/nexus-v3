"""
RAG Engine - Retrieval-Augmented Generation para SuperNEXUS v2.

Indexa documentos (repomix dumps, PLAN.md, autopsias) y permite
busqueda semantica via embeddings de nomic-embed-text (Ollama local).

Storage: SQLite (sin dependencias externas como ChromaDB).
Embeddings: nomic-embed-text via Ollama /api/embeddings.

Uso:
    engine = RAGEngine()
    await engine.initialize()
    await engine.index_file("repomix-nexus-complete.md", chunk_size=1000)
    results = await engine.search("como funciona PeerChat", top_k=5)
"""

import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import aiosqlite
import httpx

logger = logging.getLogger(__name__)

NEXUS_HOME = Path(os.environ.get("NEXUS_HOME", Path.home() / ".nexus"))
RAG_DB_PATH = NEXUS_HOME / "rag_index.db"
NEXUS_HOME.mkdir(parents=True, exist_ok=True)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_CHUNK_SIZE = 800  # chars per chunk
DEFAULT_CHUNK_OVERLAP = 100


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _chunk_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks, respecting line boundaries."""
    lines = text.split("\n")
    chunks = []
    current = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > chunk_size and current:
            chunks.append("\n".join(current))
            # Keep last N chars as overlap
            overlap_lines = []
            overlap_len = 0
            for prev_line in reversed(current):
                if overlap_len + len(prev_line) > overlap:
                    break
                overlap_lines.insert(0, prev_line)
                overlap_len += len(prev_line) + 1
            current = overlap_lines
            current_len = overlap_len
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks


class RAGEngine:
    """Motor RAG con SQLite + Ollama embeddings."""

    def __init__(
        self,
        db_path: str = str(RAG_DB_PATH),
        ollama_url: str = DEFAULT_OLLAMA_URL,
        embed_model: str = DEFAULT_EMBED_MODEL,
    ):
        self.db_path = db_path
        self.ollama_url = ollama_url
        self.embed_model = embed_model
        self._client = httpx.AsyncClient(timeout=30)
        self._db: Optional[aiosqlite.Connection] = None
        self._embed_cache: Dict[str, List[float]] = {}

    async def initialize(self):
        """Crea tablas si no existen."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE,
                embedding BLOB,
                metadata TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                path TEXT PRIMARY KEY,
                chunk_count INTEGER NOT NULL,
                indexed_at REAL NOT NULL,
                file_hash TEXT NOT NULL
            )
        """)
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
        await self._db.commit()
        logger.info(f"RAGEngine initialized: {self.db_path}")

    async def close(self):
        if self._db:
            await self._db.close()

    async def _embed(self, text: str) -> List[float]:
        """Get embedding from Ollama."""
        text_hash = hashlib.md5(text[:512].encode()).hexdigest()
        if text_hash in self._embed_cache:
            return self._embed_cache[text_hash]

        try:
            r = await self._client.post(
                f"{self.ollama_url}/api/embeddings",
                json={"model": self.embed_model, "prompt": text[:512]},
            )
            if r.status_code == 200:
                emb = r.json().get("embedding", [])
                self._embed_cache[text_hash] = emb
                return emb
        except Exception as e:
            logger.warning(f"Embedding failed: {e}")
        return []

    async def index_file(
        self,
        file_path: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
        metadata: Optional[Dict] = None,
    ) -> Dict:
        """Indexa un archivo completo en chunks con embeddings."""
        path = Path(file_path)
        if not path.exists():
            return {"error": f"File not found: {file_path}"}

        content = path.read_text(encoding="utf-8", errors="ignore")
        file_hash = hashlib.md5(content.encode()).hexdigest()

        # Check if already indexed with same hash
        async with self._db.execute(
            "SELECT file_hash FROM sources WHERE path = ?", (str(path),)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] == file_hash:
                return {"status": "already_indexed", "path": str(path)}

        # Remove old chunks for this source
        await self._db.execute("DELETE FROM chunks WHERE source = ?", (str(path),))

        chunks = _chunk_text(content, chunk_size, overlap)
        meta_str = json.dumps(metadata or {})
        indexed = 0
        errors = 0

        for i, chunk in enumerate(chunks):
            chunk_hash = hashlib.md5(f"{path}:{i}:{chunk}".encode()).hexdigest()
            embedding = await self._embed(chunk)
            if not embedding:
                errors += 1
                continue

            emb_blob = json.dumps(embedding).encode()
            try:
                await self._db.execute(
                    """INSERT OR REPLACE INTO chunks
                       (source, chunk_index, content, content_hash, embedding, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (str(path), i, chunk, chunk_hash, emb_blob, meta_str, time.time()),
                )
                indexed += 1
            except Exception as e:
                logger.warning(f"Failed to index chunk {i}: {e}")
                errors += 1

        await self._db.execute(
            """INSERT OR REPLACE INTO sources (path, chunk_count, indexed_at, file_hash)
               VALUES (?, ?, ?, ?)""",
            (str(path), indexed, time.time(), file_hash),
        )
        await self._db.commit()

        logger.info(f"Indexed {indexed}/{len(chunks)} chunks from {path.name} ({errors} errors)")
        return {
            "status": "indexed",
            "path": str(path),
            "chunks": indexed,
            "total": len(chunks),
            "errors": errors,
        }

    async def search(
        self,
        query: str,
        top_k: int = 5,
        source_filter: Optional[str] = None,
        min_score: float = 0.3,
    ) -> List[Dict]:
        """Busqueda semantica por cosine similarity."""
        query_emb = await self._embed(query)
        if not query_emb:
            return []

        sql = "SELECT id, source, chunk_index, content, embedding, metadata FROM chunks"
        params = []
        if source_filter:
            sql += " WHERE source LIKE ?"
            params.append(f"%{source_filter}%")

        results = []
        async with self._db.execute(sql, params) as cursor:
            async for row in cursor:
                chunk_id, source, idx, content, emb_blob, meta = row
                try:
                    chunk_emb = json.loads(emb_blob)
                except (json.JSONDecodeError, TypeError):
                    continue

                score = _cosine_similarity(query_emb, chunk_emb)
                if score >= min_score:
                    results.append({
                        "id": chunk_id,
                        "source": source,
                        "chunk_index": idx,
                        "content": content[:500],
                        "score": round(score, 4),
                        "metadata": meta,
                    })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    async def index_directory(
        self,
        dir_path: str,
        patterns: List[str] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> Dict:
        """Indexa todos los archivos matching en un directorio."""
        if patterns is None:
            patterns = ["*.md", "*.py", "*.txt"]

        path = Path(dir_path)
        results = {"indexed": 0, "skipped": 0, "errors": 0, "files": []}

        for pattern in patterns:
            for file_path in path.glob(pattern):
                if file_path.is_file() and file_path.stat().st_size < 5_000_000:  # 5MB max
                    r = await self.index_file(str(file_path), chunk_size)
                    if r.get("status") == "indexed":
                        results["indexed"] += 1
                    elif r.get("status") == "already_indexed":
                        results["skipped"] += 1
                    else:
                        results["errors"] += 1
                    results["files"].append(r)

        return results

    async def stats(self) -> Dict:
        """Estadisticas del indice."""
        chunks = 0
        sources = 0
        async with self._db.execute("SELECT COUNT(*) FROM chunks") as c:
            chunks = (await c.fetchone())[0]
        async with self._db.execute("SELECT COUNT(*) FROM sources") as c:
            sources = (await c.fetchone())[0]
        return {
            "chunks": chunks,
            "sources": sources,
            "db_path": self.db_path,
            "embed_model": self.embed_model,
        }

    async def delete_source(self, source_path: str) -> Dict:
        """Elimina un source y sus chunks."""
        await self._db.execute("DELETE FROM chunks WHERE source = ?", (source_path,))
        await self._db.execute("DELETE FROM sources WHERE path = ?", (source_path,))
        await self._db.commit()
        return {"deleted": source_path}
