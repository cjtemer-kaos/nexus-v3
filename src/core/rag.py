"""RAG v3 — SQLite + embeddings del servidor :8001 (nomic-embed, autocontenido).

- Chunks de 800 chars con overlap 100 (igual que v2).
- Embeddings cacheados por hash MD5.
- Búsqueda por coseno con matriz numpy cacheada en proceso (fallback Python puro).
- Fuente principal: nodos de knowledge_graph.db (cerebro propio v3).
"""
from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import threading
import time
from pathlib import Path

from .embeddings import embed

CHUNK_SIZE = 800
OVERLAP = 100

# — Background indexing state
_index_job: dict = {"running": False, "total": 0, "done": 0, "errors": 0, "last_error": None, "started_at": None, "finished_at": None}
_index_lock = threading.Lock()
_index_thread: threading.Thread | None = None


def project_root() -> Path:
    env = os.environ.get("NEXUS_ROOT", "")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).parents[2].resolve()


def rag_db_path() -> Path:
    # Keep every brain artifact under the same configured NEXUS_BRAIN root.
    from src.brain.link import brain_path
    return brain_path() / "rag_index.db"


def _connect() -> sqlite3.Connection:
    p = rag_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    # WAL + NORMAL: el indice RAG se reescribe por lotes; con FULL cada commit paga
    # fsync (~28 ms en D:\ias, medido 2026-10-26).
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            embedding BLOB,
            created_at REAL NOT NULL
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
    con.commit()
    return con


def _chunk(text: str, size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[str]:
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return [c for c in out if c.strip()]


def _cos(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if not n:
        return 0.0
    dot = sum(x * y for x, y in zip(a[:n], b[:n]))
    na = math.sqrt(sum(x * x for x in a[:n])) or 1.0
    nb = math.sqrt(sum(y * y for y in b[:n])) or 1.0
    return dot / (na * nb)


def _vec_to_blob(v: list[float]) -> bytes:
    import struct
    return struct.pack(f"{len(v)}f", *v)


def _blob_to_vec(b: bytes) -> list[float]:
    import struct
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b[:n * 4]))


def _db_clean_stale(db: sqlite3.Connection, keep: dict, drop_missing: bool = False) -> int:
    """Purga chunks huérfanos tras (re)indexar (fix #19).

    keep: {source: hashes vigentes} tras este indexado.
    - Por fuente en keep: borra sus chunks cuyo hash ya no vigente.
    - drop_missing (solo full index): borra por completo fuentes graph: ausentes.
    - Commit solo si borró algo. Devuelve nº de chunks borrados.
    """
    if not keep:
        return 0
    deleted = 0
    sources = list(keep)
    for i in range(0, len(sources), 80):
        batch = sources[i:i + 80]
        marks = ",".join("?" * len(batch))
        rows = db.execute(
            f"SELECT id, source, content_hash FROM chunks WHERE source IN ({marks})",
            batch,
        ).fetchall()
        stale = [rid for rid, src, h in rows if h not in keep.get(src, ())]
        for j in range(0, len(stale), 500):
            ids = stale[j:j + 500]
            cur = db.execute(
                f"DELETE FROM chunks WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
            deleted += cur.rowcount or 0
    if drop_missing:
        have = {r[0] for r in db.execute(
            "SELECT DISTINCT source FROM chunks WHERE source LIKE 'graph:%'"
        ).fetchall()}
        for src in sorted(have - set(keep)):
            cur = db.execute("DELETE FROM chunks WHERE source=?", (src,))
            deleted += cur.rowcount or 0
    if deleted:
        db.commit()
    return deleted


def index_graph(limit: int = 0) -> dict:
    """Indexa nodos del knowledge_graph propio a chunks con embeddings. Batched (8 por request) para reducir RTT."""
    from src.brain.link import brain_path
    gpath = brain_path() / "knowledge_graph.db"
    if not gpath.exists():
        return {"ok": False, "error": f"knowledge_graph.db no existe en {brain_path()}"}
    con = sqlite3.connect(str(gpath), timeout=10)
    con.row_factory = sqlite3.Row
    q = "SELECT id, title, content FROM nodes ORDER BY id"
    if limit and limit > 0:
        q += f" LIMIT {int(limit)}"
    rows = con.execute(q).fetchall()
    con.close()
    db = _connect()
    # recolectar pendientes primero (sin embed aún)
    pending: list[tuple[str, int, str, str]] = []  # (source, ci, content, hash)
    skipped = 0
    keep: dict[str, set[str]] = {}  # fix #19: hashes vigentes por fuente
    for r in rows:
        text = f"{r['title'] or ''}\n{r['content'] or ''}".strip()
        if not text:
            continue
        src = f"graph:{r['id']}"
        hashes = keep.setdefault(src, set())
        for ci, ch in enumerate(_chunk(text)):
            h = hashlib.md5(ch.encode()).hexdigest()
            hashes.add(h)
            if db.execute("SELECT 1 FROM chunks WHERE content_hash=?", (h,)).fetchone():
                skipped += 1
                continue
            pending.append((src, ci, ch, h))
    if not pending:
        # fix #19: sin inserts nuevos, aún así purgar huérfanos de estas fuentes
        stale = _db_clean_stale(db, keep, drop_missing=not limit)
        total = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        db.close()
        return {"ok": True, "nodes": len(rows), "new_chunks": 0, "skipped": skipped, "total_chunks": total, "stale_chunks": stale}
    # embed en batches de 8 (una sola request HTTP por batch)
    BATCH = 8
    new_chunks = 0
    for i in range(0, len(pending), BATCH):
        batch = pending[i:i + BATCH]
        texts = [c for _, _, c, _ in batch]
        vecs = embed(texts)
        if not vecs or len(vecs) != len(batch):
            db.close()
            return {"ok": False, "error": "embed :8001 no responde o batch incompleto", "new_chunks": new_chunks, "skipped": skipped, "stale_chunks": 0}
        for (src, ci, ch, h), v in zip(batch, vecs):
            db.execute(
                "INSERT INTO chunks (source, chunk_index, content, content_hash, embedding, created_at) VALUES (?,?,?,?,?,?)",
                (src, ci, ch, h, _vec_to_blob(v), time.time()),
            )
            new_chunks += 1
        db.commit()
        # actualizar progreso background si está corriendo
        if _index_job.get("running"):
            with _index_lock:
                _index_job["done"] = skipped + new_chunks
    # fix #19: purgar huérfanos solo si el embed terminó sin error
    stale = _db_clean_stale(db, keep, drop_missing=not limit)
    total = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    db.close()
    return {"ok": True, "nodes": len(rows), "new_chunks": new_chunks, "skipped": skipped, "total_chunks": total, "stale_chunks": stale}


def index_graph_background(limit: int = 0) -> dict:
    """Lanza index_graph en thread background. Retorna estado inmediato. Seguro contra doble arranque."""
    global _index_thread
    with _index_lock:
        if _index_job["running"]:
            return {"ok": True, "background": True, "already_running": True, **_index_job}
        # estimar total
        from src.brain.link import brain_path as _bp
        try:
            gpath = _bp() / "knowledge_graph.db"
            con = sqlite3.connect(str(gpath), timeout=5)
            tot = con.execute("SELECT count(*) FROM nodes").fetchone()[0] if gpath.exists() else 0
            con.close()
        except Exception:
            tot = 0
        _index_job.update({"running": True, "total": tot, "done": 0, "errors": 0, "last_error": None, "started_at": time.time(), "finished_at": None})

    def _run():
        try:
            res = index_graph(limit=limit)
            with _index_lock:
                if not res.get("ok"):
                    _index_job["errors"] += 1
                    _index_job["last_error"] = res.get("error")
                _index_job["finished_at"] = time.time()
        except Exception as e:
            with _index_lock:
                _index_job["errors"] += 1
                _index_job["last_error"] = str(e)
                _index_job["finished_at"] = time.time()
        finally:
            with _index_lock:
                _index_job["running"] = False

    _index_thread = threading.Thread(target=_run, daemon=True)
    _index_thread.start()
    with _index_lock:
        return {"ok": True, "background": True, "already_running": False, **dict(_index_job)}


def index_status() -> dict:
    with _index_lock:
        d = dict(_index_job)
    s = stats()
    d["chunks_in_db"] = s["chunks"]
    if d["running"] and d["total"]:
        d["progress_pct"] = round(d["done"] / max(1, d["total"]) * 100, 1)
    return d


def _mmr_rerank(scored: list[tuple], limit: int, lam: float = 0.7) -> list[tuple]:
    """Diversifica el top-k (MMR con Jaccard de tokens, sin embeddings).

    Sin esto, 5 chunks casi iguales del mismo archivo saturan el contexto.
    """
    import re as _re

    def toks(text: str) -> set[str]:
        return set(_re.findall(r"\w+", (text or "").lower()))

    pool = [(s, src, content, toks(content)) for s, src, content in scored[: max(limit * 4, limit)]]
    chosen: list[tuple] = []
    while pool and len(chosen) < limit:
        best_i, best_val = -1, float("-inf")
        for i, (s, _src, _content, tk) in enumerate(pool):
            overlap = max((len(tk & c[3]) / max(1, len(tk | c[3])) for c in chosen), default=0.0)
            val = lam * float(s) - (1.0 - lam) * overlap
            if val > best_val:
                best_val, best_i = val, i
        if best_i < 0:
            break
        chosen.append(pool.pop(best_i))
    return [(s, src, content) for s, src, content, _ in chosen]


_index_cache: dict = {"key": None, "matrix": None, "sources": None, "contents": None}
_index_lock = threading.Lock()


def _index_key(db: sqlite3.Connection):
    """Clave barata de invalidacion: cantidad de chunks + ultimo id."""
    try:
        row = db.execute("SELECT count(*), coalesce(max(id), 0) FROM chunks").fetchone()
        return (int(row[0] or 0), int(row[1] or 0))
    except Exception:
        return None


def _cached_index(db: sqlite3.Connection):
    """Matriz (N, dim) normalizada + fuentes + contenidos, cacheada en proceso.

    Devuelve None si numpy no esta disponible o no hay chunks: ahi search cae al
    coseno en Python puro de antes.
    """
    key = _index_key(db)
    if key is None:
        return None
    with _index_lock:
        if _index_cache["key"] == key and _index_cache["matrix"] is not None:
            return _index_cache["matrix"], _index_cache["sources"], _index_cache["contents"]
    try:
        import numpy as np
    except Exception:
        return None
    rows = db.execute(
        "SELECT source, content, embedding FROM chunks WHERE embedding IS NOT NULL"
    ).fetchall()
    if not rows:
        return None
    dim = min((len(b) // 4) for _, _, b in rows if b) if any(b for _, _, b in rows) else 0
    if dim <= 0:
        return None
    mat = np.zeros((len(rows), dim), dtype=np.float32)
    sources: list[str] = []
    contents: list[str] = []
    for i, (src, content, blob) in enumerate(rows):
        if blob:
            mat[i] = np.frombuffer(blob[: dim * 4], dtype=np.float32)
        sources.append(src)
        contents.append(content)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    mat /= norms[:, None]
    with _index_lock:
        _index_cache.update(
            {"key": key, "matrix": mat, "sources": sources, "contents": contents}
        )
    return mat, sources, contents


def _score_numpy(index, qv: list[float], min_score: float, top: int = 200) -> list[tuple]:
    """Coseno vectorizado. Devuelve top-N (score, source, content) ya ordenado."""
    import numpy as np
    mat, sources, contents = index
    q = np.asarray(qv, dtype=np.float32)[: mat.shape[1]]
    n = float(np.linalg.norm(q))
    if n == 0 or q.size == 0:
        return []
    sims = mat @ (q / n)
    keep = np.flatnonzero(sims >= min_score)
    if keep.size == 0:
        return []
    keep = keep[np.argsort(-sims[keep])][:top]
    return [(float(sims[i]), sources[i], contents[i]) for i in keep]


def search(query: str, limit: int = 5, min_score: float = 0.25) -> list[dict]:
    """Búsqueda semántica sobre el índice RAG. Descarta scores < min_score (fix: ruido ~0.05 pasaba).

    Con 19k chunks el coseno en Python puro tardaba 3.5-12 s por query. La matriz de
    embeddings se arma una vez y se cachea en proceso (invalidada por count+max(id)).
    """
    qvecs = embed(query)
    if not qvecs:
        return []
    qv = qvecs[0]
    db = _connect()
    try:
        idx = _cached_index(db)
    finally:
        db.close()
    if idx is not None:
        scored = _score_numpy(idx, qv, min_score)
    else:
        db = _connect()
        try:
            rows = db.execute(
                "SELECT source, content, embedding FROM chunks"
            ).fetchall()
        finally:
            db.close()
        scored = []
        for src, content, blob in rows:
            if not blob:
                continue
            s = _cos(qv, _blob_to_vec(blob))
            if s >= min_score:
                scored.append((s, src, content))
        scored.sort(key=lambda x: -x[0])
    if len(scored) > limit:
        scored = _mmr_rerank(scored, limit)
    return [{"score": round(s, 4), "source": src, "content": content[:600]} for s, src, content in scored[:limit]]


def stats() -> dict:
    db = _connect()
    total = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    db.close()
    return {"chunks": total, "db": str(rag_db_path()), "exists": rag_db_path().exists()}
