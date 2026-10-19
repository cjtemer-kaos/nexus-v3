"""KnowledgeMemory — Método OPCODE aplicado al grafo de nexus-v3.

Vía ÚNICA de escritura de conocimiento (paridad con v2 AGENTS.md § Método OPCODE):

  learn() -> 4 destinos del pipeline:
    1. grafo  : knowledge_graph.db (nodes upsert por topic_key + FTS5 + entities
                + auto-conexión shared_entity con tope 12 aristas/nodo)
    2. rag    : chunks+embeddings en rag_index.db (best-effort, :8001)
    3. markdown: brain/learnings.md (append)
    4. observation: hybrid_memory.db (best-effort, en el brain local)

Reglas:
  - topic_key -> UPSERT (revision_count++) en vez de filas duplicadas.
  - sin topic_key -> dedupe por content_hash (ventana 24h).
  - Nodes are durable and have no fixed count/age cap; maintenance removes only
    exact duplicates while preserving unique knowledge nodes.
  - Edges have bounded growth (60k total, 12 per node); maintenance removes
    weakest shared-entity links before it ever drops a semantic insight edge.

Sin dependencias de v2: código autónomo de nexus-v3 (BLUEPRINT §11).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Nodes are deliberately unbounded; the edge ceiling follows OPCODE's v2
# brain_guard limit and only protects against runaway relationship growth.
MAX_EDGES = 60000
MAX_EDGES_PER_NODE = 12
DEDUPE_WINDOW_H = 24
MAX_ENTITIES_PER_NODE = 12
AUTO_LINKS_PER_ENTITY = 2

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "its", "into", "than", "then", "them",
    "they", "will", "would", "could", "should", "can", "may", "use", "using",
    "new", "one", "two", "all", "any", "each", "every", "via", "via.", "sin",
    "para", "como", "más", "mas", "una", "uno", "los", "las", "del", "por",
    "con", "que", "se", "no", "es", "en",
}

_ENTITY_PATTERNS = [
    re.compile(r'"([^"\n]{3,60})"'),
    re.compile(r"'([^'\n]{3,60})'"),
    re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b"),   # CamelCase
    re.compile(r"\b([A-Z]{3,8})\b"),                            # ACRÓNIMO
    re.compile(r"\b((?:[A-Z][\w]+)(?:\s+(?:[A-Z][\w]+)){1,3})\b"),  # Nombre Propio multi-palabra
]

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_DONE: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha1(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()


def _extract_entities(text: str, limit: int = MAX_ENTITIES_PER_NODE) -> list[str]:
    """Entidades deterministas: quoted, CamelCase, ACRÓNIMOS, Nombres Propios."""
    if not text:
        return []
    seen: dict[str, str] = {}
    for pat in _ENTITY_PATTERNS:
        for m in pat.finditer(text):
            raw = " ".join(m.group(1).split())
            if len(raw) < 3 or raw.lower() in _STOPWORDS:
                continue
            if raw.isdigit():
                continue
            key = raw.lower()
            if key not in seen:
                seen[key] = raw
            if len(seen) >= limit:
                return list(seen.values())
    return list(seen.values())


class KnowledgeMemory:
    """Grafo FTS5 con upsert por topic_key — el 'knowledge_memory' de v3."""

    def __init__(self, db_path: Optional[Path] = None, *, rag_index: bool = True):
        if db_path is None:
            from src.brain.link import brain_path
            db_path = brain_path() / "knowledge_graph.db"
        self.db_path = Path(db_path)
        self.rag_index = rag_index
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_key = str(self.db_path)
        self.ensure_schema()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=30.0)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA busy_timeout=5000;")
            con.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def ensure_schema(self) -> None:
        """Migración idempotente: topic_key, revision_count, FTS5, entities."""
        with _SCHEMA_LOCK:
            if self._schema_key in _SCHEMA_DONE:
                return
            con = self._connect()
            try:
                cols = {r["name"] for r in con.execute("PRAGMA table_info(nodes)")}
                if not cols:
                    con.execute(
                        """CREATE TABLE IF NOT EXISTS nodes (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            content TEXT NOT NULL,
                            title TEXT NOT NULL DEFAULT '',
                            category TEXT NOT NULL DEFAULT 'general',
                            source TEXT NOT NULL DEFAULT 'nexus',
                            importance INTEGER NOT NULL DEFAULT 5,
                            tags TEXT NOT NULL DEFAULT '[]',
                            content_hash TEXT NOT NULL DEFAULT '',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            access_count INTEGER NOT NULL DEFAULT 0,
                            last_accessed TEXT
                        )"""
                    )
                    cols = {r["name"] for r in con.execute("PRAGMA table_info(nodes)")}
                if "topic_key" not in cols:
                    con.execute("ALTER TABLE nodes ADD COLUMN topic_key TEXT NOT NULL DEFAULT ''")
                if "revision_count" not in cols:
                    con.execute("ALTER TABLE nodes ADD COLUMN revision_count INTEGER NOT NULL DEFAULT 0")
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_nodes_topic_key ON nodes(topic_key)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_nodes_content_hash ON nodes(content_hash)"
                )
                con.execute(
                    """CREATE TABLE IF NOT EXISTS edges (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        from_node INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                        to_node INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                        relation TEXT NOT NULL DEFAULT 'relates_to',
                        weight REAL NOT NULL DEFAULT 1.0,
                        note TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT '',
                        UNIQUE(from_node, to_node, relation)
                    )"""
                )
                con.execute("CREATE INDEX IF NOT EXISTS idx_edges_from_node ON edges(from_node)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_edges_to_node ON edges(to_node)")
                # Historial de revisiones (supersede-not-delete): al pisar el
                # contenido de un nodo con topic_key, la version saliente se
                # archiva aca en vez de perderse. Restaurarla es escribir una
                # version nueva, asi que el historial nunca se recorta.
                con.execute(
                    """CREATE TABLE IF NOT EXISTS node_revisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                        revision INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        category TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        importance INTEGER NOT NULL DEFAULT 5,
                        tags TEXT NOT NULL DEFAULT '[]',
                        content_hash TEXT NOT NULL DEFAULT '',
                        archived_at TEXT NOT NULL DEFAULT '',
                        UNIQUE(node_id, revision)
                    )"""
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_node_revisions_node ON node_revisions(node_id)"
                )
                con.execute(
                    """CREATE TABLE IF NOT EXISTS knowledge_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )"""
                )
                # FTS5 external content + triggers de sincronía
                con.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                        title, content, tags, category,
                        content='nodes', content_rowid='id'
                    )"""
                )
                for name, sql in (
                    (
                        "nodes_ai",
                        """CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
                            INSERT INTO nodes_fts(rowid, title, content, tags, category)
                            VALUES (new.id, new.title, new.content, new.tags, new.category);
                        END""",
                    ),
                    (
                        "nodes_ad",
                        """CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
                            INSERT INTO nodes_fts(nodes_fts, rowid, title, content, tags, category)
                            VALUES ('delete', old.id, old.title, old.content, old.tags, old.category);
                        END""",
                    ),
                    (
                        "nodes_au",
                        """CREATE TRIGGER IF NOT EXISTS nodes_au
                            AFTER UPDATE OF title, content, tags, category ON nodes BEGIN
                            INSERT INTO nodes_fts(nodes_fts, rowid, title, content, tags, category)
                            VALUES ('delete', old.id, old.title, old.content, old.tags, old.category);
                            INSERT INTO nodes_fts(rowid, title, content, tags, category)
                            VALUES (new.id, new.title, new.content, new.tags, new.category);
                        END""",
                    ),
                ):
                    con.execute(sql)
                # En FTS externo, count(*) lee la tabla de contenido aunque el
                # índice esté vacío. Una marca persistente evita ese falso positivo.
                fts_backfill = con.execute(
                    "SELECT value FROM knowledge_meta WHERE key='fts_backfilled_v1'"
                ).fetchone()
                if not fts_backfill:
                    con.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
                    con.execute(
                        "INSERT INTO knowledge_meta (key,value) VALUES ('fts_backfilled_v1',?)",
                        (_now_iso(),),
                    )
                # entities (PK = name_lower; lookup case-insensitive)
                con.execute(
                    """CREATE TABLE IF NOT EXISTS entities (
                        name TEXT NOT NULL,
                        name_lower TEXT PRIMARY KEY,
                        mention_count INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL DEFAULT ''
                    )"""
                )
                con.execute(
                    """CREATE TABLE IF NOT EXISTS entity_nodes (
                        entity_lower TEXT NOT NULL,
                        node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                        PRIMARY KEY (entity_lower, node_id)
                    )"""
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_entity_nodes_node ON entity_nodes(node_id)"
                )
                # Schema tolerante: el brain puede ser compartido con otro esquema
                # (p.ej. v2: entities(id,name,entity_type,node_ids,mention_count)).
                # Agrega las columnas que falten para no romper recall/learn.
                ecols = {r["name"] for r in con.execute("PRAGMA table_info(entities)")}
                if "name_lower" not in ecols:
                    con.execute("ALTER TABLE entities ADD COLUMN name_lower TEXT")
                if "updated_at" not in ecols:
                    con.execute("ALTER TABLE entities ADD COLUMN updated_at TEXT DEFAULT ''")
                # Indexar las entidades de los nodos ya migrados para que las
                # nuevas memorias puedan enlazarse con el grafo preexistente.
                backfill = con.execute(
                    "SELECT value FROM knowledge_meta WHERE key='entities_backfilled_v1'"
                ).fetchone()
                if not backfill:
                    try:
                        old_nodes = con.execute(
                            "SELECT id, title, content FROM nodes ORDER BY id"
                        ).fetchall()
                        for old_node in old_nodes:
                            node_id = int(old_node["id"])
                            text = f"{old_node['title'] or ''}\n{(old_node['content'] or '')[:4000]}"
                            for ent in _extract_entities(text):
                                el = ent.lower()
                                con.execute(
                                    "INSERT OR IGNORE INTO entities "
                                    "(name, name_lower, mention_count, updated_at) VALUES (?,?,0,?)",
                                    (ent, el, _now_iso()),
                                )
                                cur = con.execute(
                                    "INSERT OR IGNORE INTO entity_nodes (entity_lower,node_id) VALUES (?,?)",
                                    (el, node_id),
                                )
                                if cur.rowcount:
                                    con.execute(
                                        "UPDATE entities SET mention_count=mention_count+1 "
                                        "WHERE name_lower=?", (el,),
                                    )
                        con.execute(
                            "INSERT INTO knowledge_meta (key,value) VALUES ('entities_backfilled_v1',?)",
                            (_now_iso(),),
                        )
                    except Exception as _e:
                        logger.warning("entities backfill omitido (schema ajeno): %s", _e)
                # edges: garantiza columna created_at (migración vieja)
                ecols = {r["name"] for r in con.execute("PRAGMA table_info(edges)")}
                if ecols and "created_at" not in ecols:
                    con.execute("ALTER TABLE edges ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
                con.commit()
                _SCHEMA_DONE.add(self._schema_key)
            finally:
                con.close()

    # ------------------------------------------------------------------
    # learn — la vía única
    # ------------------------------------------------------------------
    def learn(
        self,
        content: str,
        *,
        topic_key: str = "",
        category: str = "general",
        tags: Optional[list[str]] = None,
        title: str = "",
        source: str = "api",
        importance: int = 5,
        index_rag: Optional[bool] = None,
        persist_markdown: bool = True,
        persist_observation: bool = True,
    ) -> dict[str, Any]:
        """4 destinos del pipeline; solo el grafo es obligatorio.

        RAG, markdown y observation reportan fallos sin descartar el nodo del
        grafo ni romper el turno que llamó a learn().
        """
        content = (content or "").strip()
        if not content:
            raise ValueError("content requerido")
        topic_key = (topic_key or "").strip()
        tags = tags or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        category = (category or "general").strip() or "general"
        title = (title or "").strip() or content.split("\n")[0][:80]
        chash = _sha1(content)

        # --- salida 1: grafo (upsert + entities + auto-edges)
        graph_out = self._graph_upsert(
            content=content, topic_key=topic_key, category=category,
            tags=tags, title=title, source=source, importance=importance,
            chash=chash,
        )
        if graph_out.get("deduped"):
            return {
                "ok": True, "deduped": True, "node_id": graph_out["node_id"],
                "outputs": {"graph": "deduped"},
            }
        node_id = graph_out["node_id"]

        # --- salida 3: markdown learnings.md
        md_ok, md_err = (self._append_markdown(topic_key or category, title, node_id)
                         if persist_markdown else (False, "disabled"))

        # --- salida 2: RAG (best-effort)
        rag_ok, rag_err = (False, "disabled")
        should_index_rag = self.rag_index if index_rag is None else index_rag
        if should_index_rag:
            rag_ok, rag_err = self._rag_index(node_id, title, content)

        # --- salida 4: observation (best-effort, hybrid_memory.db local)
        obs_ok, obs_err, obs_id = self._observation(
            content, category=category, topic_key=topic_key,
            source=source, persist=persist_observation,
        )

        return {
            "ok": True,
            "node_id": node_id,
            "topic_key": topic_key or None,
            "revision": graph_out["revision"],
            "deduped": False,
            "entities": graph_out["entities"],
            "edges_added": graph_out["edges_added"],
            "observation_id": obs_id,
            "outputs": {
                "graph": "ok",
                "rag": "ok" if rag_ok else f"skip: {rag_err}",
                "markdown": "ok" if md_ok else f"skip: {md_err}",
                "observation": "ok" if obs_ok else f"skip: {obs_err}",
            },
        }

    # ------------------------------------------------------------------
    # salidas internas
    # ------------------------------------------------------------------
    def _graph_upsert(self, *, content, topic_key, category, tags,
                      title, source, importance, chash) -> dict[str, Any]:
        con = self._connect()
        try:
            # Serializa topic UPSERT y dedupe sin topic_key para que dos
            # escritores concurrentes no creen duplicados.
            con.execute("BEGIN IMMEDIATE")
            now = _now_iso()
            tags_json = json.dumps(tags, ensure_ascii=False)
            revision = 1
            if not topic_key:
                recent = con.execute(
                    "SELECT id FROM nodes WHERE content_hash=? AND topic_key='' "
                    "AND updated_at >= ? ORDER BY id DESC LIMIT 1",
                    (chash, _iso_since(DEDUPE_WINDOW_H)),
                ).fetchone()
                if recent:
                    return {"deduped": True, "node_id": int(recent["id"])}
            if topic_key:
                row = con.execute(
                    "SELECT id, revision_count, content, title, category, source,"
                    " importance, tags, content_hash FROM nodes WHERE topic_key=? ORDER BY id LIMIT 1",
                    (topic_key,),
                ).fetchone()
                if row:
                    node_id = int(row["id"])
                    prev_revision = int(row["revision_count"] or 0)
                    revision = prev_revision + 1
                    # supersede-not-delete: la version que sale queda archivada
                    # (y por lo tanto restaurable) antes de pisarla.
                    con.execute(
                        """INSERT OR IGNORE INTO node_revisions
                           (node_id, revision, content, title, category, source,
                            importance, tags, content_hash, archived_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (node_id, prev_revision, row["content"], row["title"],
                         row["category"], row["source"], int(row["importance"] or 5),
                         row["tags"], row["content_hash"], now),
                    )
                    con.execute(
                        """UPDATE nodes SET content=?, title=?, category=?, tags=?,
                           content_hash=?, updated_at=?, revision_count=?,
                           importance=MAX(importance, ?), source=?
                           WHERE id=?""",
                        (content, title, category, tags_json, chash, now,
                         revision, int(importance or 5), source, node_id),
                    )
                else:
                    cur = con.execute(
                        """INSERT INTO nodes (content, title, category, source,
                           importance, tags, content_hash, created_at, updated_at,
                           access_count, topic_key, revision_count)
                           VALUES (?,?,?,?,?,?,?,?,?,0,?,1)""",
                        (content, title, category, source, int(importance or 5),
                         tags_json, chash, now, now, topic_key),
                    )
                    node_id = int(cur.lastrowid)
            else:
                cur = con.execute(
                    """INSERT INTO nodes (content, title, category, source,
                       importance, tags, content_hash, created_at, updated_at,
                       access_count, topic_key, revision_count)
                       VALUES (?,?,?,?,?,?,?,?,?,0,'',1)""",
                    (content, title, category, source, int(importance or 5),
                     tags_json, chash, now, now),
                )
                node_id = int(cur.lastrowid)

            # entities + auto-conexión shared_entity
            entities = _extract_entities(f"{title}\n{content}")
            edges_added = 0
            degree = con.execute(
                "SELECT count(*) FROM edges WHERE from_node=? OR to_node=?",
                (node_id, node_id),
            ).fetchone()[0]
            for ent in entities[:MAX_ENTITIES_PER_NODE]:
                el = ent.lower()
                try:
                    con.execute(
                        "INSERT OR IGNORE INTO entities "
                        "(name, name_lower, mention_count, updated_at) VALUES (?,?,0,?)",
                        (ent, el, now),
                    )
                    entity_link = con.execute(
                        "INSERT OR IGNORE INTO entity_nodes (entity_lower, node_id) VALUES (?,?)",
                        (el, node_id),
                    )
                    if entity_link.rowcount:
                        con.execute(
                            "UPDATE entities SET mention_count=mention_count+1, updated_at=?"
                            " WHERE name_lower=?",
                            (now, el),
                        )
                except Exception as _e:
                    logger.debug("entities insert omitido (schema ajeno): %s", _e)
                if degree >= MAX_EDGES_PER_NODE:
                    continue
                partners = con.execute(
                    """SELECT en.node_id FROM entity_nodes en
                       JOIN nodes n ON n.id = en.node_id
                       WHERE en.entity_lower=? AND en.node_id != ?
                       ORDER BY n.updated_at DESC, n.id DESC LIMIT ?""",
                    (el, node_id, MAX_EDGES_PER_NODE + AUTO_LINKS_PER_ENTITY),
                ).fetchall()
                for p in partners:
                    if degree >= MAX_EDGES_PER_NODE:
                        break
                    other = int(p["node_id"])
                    pdeg = con.execute(
                        "SELECT count(*) FROM edges WHERE from_node=? OR to_node=?",
                        (other, other),
                    ).fetchone()[0]
                    if pdeg >= MAX_EDGES_PER_NODE:
                        continue
                    source_id, target_id = sorted((node_id, other))
                    cur = con.execute(
                        """INSERT OR IGNORE INTO edges
                           (from_node, to_node, relation, weight, note, created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (source_id, target_id, "shared_entity", 0.5,
                         f"shared entity: {ent}", now),
                    )
                    if cur.rowcount:
                        edges_added += 1
                        degree += 1
            con.commit()
            return {"node_id": node_id, "revision": revision,
                    "entities": entities, "edges_added": edges_added}
        finally:
            con.close()

    def _append_markdown(self, key: str, title: str, node_id: int) -> tuple[bool, str]:
        try:
            md = self.db_path.parent / "learnings.md"
            line = f"- {_now_iso()} | {key} | {title} (node #{node_id})\n"
            with open(md, "a", encoding="utf-8") as f:
                f.write(line)
            return True, ""
        except Exception as e:
            return False, str(e)

    def _rag_index(self, node_id: int, title: str, content: str) -> tuple[bool, str]:
        try:
            from src.core import rag
            text = f"{title}\n{content}".strip()
            db = rag._connect()
            try:
                new = 0
                keep: set[str] = set()  # hashes vigentes de este nodo (fix #19)
                for ci, ch in enumerate(rag._chunk(text)):
                    h = hashlib.md5(ch.encode()).hexdigest()
                    keep.add(h)
                    if db.execute(
                        "SELECT 1 FROM chunks WHERE content_hash=?", (h,)
                    ).fetchone():
                        continue
                    from src.core.embeddings import embed
                    vecs = embed([ch])
                    if not vecs:
                        return False, "embed :8001 no disponible"
                    db.execute(
                        "INSERT INTO chunks (source, chunk_index, content,"
                        " content_hash, embedding, created_at) VALUES (?,?,?,?,?,?)",
                        (f"graph:{node_id}", ci, ch, h,
                         rag._vec_to_blob(vecs[0]), time.time()),
                    )
                    new += 1
                db.commit()
                # fix #19: purgar chunks de este nodo cuyo hash ya no vigente
                stale = 0
                src = f"graph:{node_id}"
                rows = db.execute(
                    "SELECT id, content_hash FROM chunks WHERE source=?", (src,)
                ).fetchall()
                stale_ids = [r[0] for r in rows if r[1] not in keep]
                for j in range(0, len(stale_ids), 500):
                    ids = stale_ids[j:j + 500]
                    cur = db.execute(
                        f"DELETE FROM chunks WHERE id IN ({','.join('?' * len(ids))})",
                        ids,
                    )
                    stale += cur.rowcount or 0
                if stale:
                    db.commit()
                msg = f"{new} chunks"
                if stale:
                    msg += f", {stale} stale"
                return True, msg
            finally:
                db.close()
        except Exception as e:
            return False, str(e)

    def _observation(self, content: str, *, category: str, topic_key: str,
                     source: str, persist: bool) -> tuple[bool, str, Optional[int]]:
        if not persist:
            return False, "disabled", None
        try:
            backend = _hybrid_backend(self.db_path.parent / "hybrid_memory.db")
            write = backend.upsert if topic_key else backend.add
            mid = write(content=content, category=category, topic_key=topic_key,
                        source=source)
            return True, "", int(mid)
        except Exception as e:
            return False, str(e), None

    # ------------------------------------------------------------------
    # consultas (paridad KnowledgeGraph v2)
    # ------------------------------------------------------------------
    def fts_search(self, query: str, limit: int = 20) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        # FTS5: escapar comillas, envolver tokens en comillas
        tokens = re.findall(r"[\w]+", query)
        if not tokens:
            return []
        match = " ".join(f'"{t}"' for t in tokens[:8])
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT n.id, n.title, n.category, n.topic_key, n.revision_count,
                          snippet(nodes_fts, 1, '[', ']', '…', 12) AS snip,
                          bm25(nodes_fts) AS rank
                   FROM nodes_fts JOIN nodes n ON n.id = nodes_fts.rowid
                   WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?""",
                (match, max(1, min(100, int(limit)))),
            ).fetchall()
            return [
                {"id": int(r["id"]), "title": r["title"], "category": r["category"],
                 "topic_key": r["topic_key"], "revision": r["revision_count"],
                 "snippet": r["snip"], "rank": r["rank"]}
                for r in rows
            ]
        finally:
            con.close()

    def find_related(self, node_id: int, limit: int = 30) -> dict:
        con = self._connect()
        try:
            node = con.execute(
                "SELECT id, title, category, topic_key FROM nodes WHERE id=?",
                (node_id,),
            ).fetchone()
            if not node:
                return {"error": "node not found", "node_id": node_id}
            rows = con.execute(
                """SELECT e.id AS edge_id, e.relation, e.weight, e.note,
                          CASE WHEN e.from_node=? THEN e.to_node ELSE e.from_node END AS other_id,
                          CASE WHEN e.from_node=? THEN 'out' ELSE 'in' END AS direction,
                          n.title, n.category
                   FROM edges e JOIN nodes n ON n.id =
                     (CASE WHEN e.from_node=? THEN e.to_node ELSE e.from_node END)
                   WHERE e.from_node=? OR e.to_node=?
                   ORDER BY e.weight DESC LIMIT ?""",
                (node_id, node_id, node_id, node_id, node_id,
                 max(1, min(200, int(limit)))),
            ).fetchall()
            return {
                "node": dict(node),
                "related": [
                    {"edge_id": int(r["edge_id"]), "relation": r["relation"],
                     "weight": r["weight"], "note": r["note"],
                     "other_id": int(r["other_id"]), "direction": r["direction"],
                     "title": r["title"], "category": r["category"]}
                    for r in rows
                ],
                "degree": len(rows),
            }
        finally:
            con.close()

    def find_path(self, a: int, b: int, max_depth: int = 4) -> dict:
        """BFS no dirigido a↔b."""
        if a == b:
            return {"found": True, "path": [a], "length": 0}
        con = self._connect()
        try:
            adj: dict[int, list[int]] = {}
            for r in con.execute("SELECT from_node, to_node FROM edges"):
                u, v = int(r["from_node"]), int(r["to_node"])
                adj.setdefault(u, []).append(v)
                adj.setdefault(v, []).append(u)
            prev: dict[int, int] = {a: -1}
            queue = [a]
            depth = 0
            while queue and depth < max_depth:
                nxt = []
                for u in queue:
                    for v in adj.get(u, ()):
                        if v not in prev:
                            prev[v] = u
                            nxt.append(v)
                            if v == b:
                                path = [b]
                                while prev[path[-1]] != -1:
                                    path.append(prev[path[-1]])
                                path.reverse()
                                return {"found": True, "path": path,
                                        "length": len(path) - 1}
                queue = nxt
                depth += 1
            return {"found": False, "path": [], "length": -1}
        finally:
            con.close()

    def get_entities(self, limit: int = 30) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT e.name, e.mention_count,
                          (SELECT count(*) FROM entity_nodes en
                           WHERE en.entity_lower = e.name_lower) AS nodes
                   FROM entities e ORDER BY e.mention_count DESC, e.name LIMIT ?""",
                (max(1, min(200, int(limit))),),
            ).fetchall()
            return [{"name": r["name"], "mentions": r["mention_count"],
                     "nodes": r["nodes"]} for r in rows]
        finally:
            con.close()

    def get_central(self, limit: int = 20) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT n.id, n.title, n.category,
                          ((SELECT count(*) FROM edges WHERE from_node=n.id)
                         + (SELECT count(*) FROM edges WHERE to_node=n.id)) AS degree
                   FROM nodes n ORDER BY degree DESC, n.id LIMIT ?""",
                (max(1, min(100, int(limit))),),
            ).fetchall()
            return [{"id": int(r["id"]), "title": r["title"],
                     "category": r["category"], "degree": r["degree"]}
                    for r in rows]
        finally:
            con.close()

    def latest_topic(self, prefix: str) -> Optional[dict[str, Any]]:
        """Latest graph node whose topic_key is inside a stable topic family."""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id, topic_key, updated_at FROM nodes "
                "WHERE topic_key LIKE ? ORDER BY updated_at DESC, id DESC LIMIT 1",
                (f"{prefix}%",),
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def add_relation(self, from_node: int, to_node: int, relation: str,
                     weight: float = 1.0, note: str = "") -> dict[str, Any]:
        """Add a typed, directed knowledge edge without capping node storage.

        If a node is at its per-node relation limit, replace its weakest
        shared-entity edge first so semantic edges such as ``supersedes`` win.
        """
        if from_node == to_node:
            return {"added": False, "reason": "self_loop"}
        relation = (relation or "relates_to").strip().lower()
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            exists = con.execute(
                "SELECT 1 FROM edges WHERE from_node=? AND to_node=? AND relation=?",
                (from_node, to_node, relation),
            ).fetchone()
            if exists:
                con.commit()
                return {"added": False, "reason": "exists"}
            edge_count = con.execute("SELECT count(*) FROM edges").fetchone()[0]
            if edge_count >= MAX_EDGES:
                weakest_global = con.execute(
                    """SELECT id FROM edges WHERE relation='shared_entity'
                       ORDER BY weight ASC, created_at ASC, id ASC LIMIT 1"""
                ).fetchone()
                if weakest_global:
                    con.execute("DELETE FROM edges WHERE id=?", (weakest_global["id"],))
                else:
                    con.rollback()
                    return {"added": False, "reason": "edge_cap"}
            for node_id in (from_node, to_node):
                degree = con.execute(
                    "SELECT count(*) FROM edges WHERE from_node=? OR to_node=?",
                    (node_id, node_id),
                ).fetchone()[0]
                if degree >= MAX_EDGES_PER_NODE:
                    weakest = con.execute(
                        """SELECT id FROM edges WHERE relation='shared_entity'
                           AND (from_node=? OR to_node=?)
                           ORDER BY weight ASC, created_at ASC, id ASC LIMIT 1""",
                        (node_id, node_id),
                    ).fetchone()
                    if weakest:
                        con.execute("DELETE FROM edges WHERE id=?", (weakest["id"],))
                    else:
                        con.rollback()
                        return {"added": False, "reason": "degree_cap"}
            cur = con.execute(
                """INSERT OR IGNORE INTO edges
                   (from_node,to_node,relation,weight,note,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (from_node, to_node, relation, float(weight), note, _now_iso()),
            )
            con.commit()
            return {"added": bool(cur.rowcount), "reason": "ok" if cur.rowcount else "exists"}
        finally:
            con.close()

    # ------------------------------------------------------------------
    # historial de revisiones (supersede-not-delete)
    # ------------------------------------------------------------------
    def node_history(self, node_id: int, limit: int = 50) -> dict[str, Any]:
        """Version actual del nodo + sus revisiones archivadas (nuevas primero)."""
        con = self._connect()
        try:
            cur = con.execute(
                "SELECT id, title, category, source, importance, tags, content,"
                " content_hash, topic_key, revision_count, created_at, updated_at"
                " FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not cur:
                return {"error": "node not found", "node_id": node_id}
            rows = con.execute(
                "SELECT revision, title, category, source, importance, tags, content,"
                " content_hash, archived_at FROM node_revisions"
                " WHERE node_id=? ORDER BY revision DESC LIMIT ?",
                (node_id, max(1, min(200, int(limit)))),
            ).fetchall()
            total = con.execute(
                "SELECT count(*) FROM node_revisions WHERE node_id=?", (node_id,)
            ).fetchone()[0]
            return {
                "node_id": node_id,
                "topic_key": cur["topic_key"],
                "current": {
                    "revision": int(cur["revision_count"] or 0),
                    "title": cur["title"], "category": cur["category"],
                    "source": cur["source"], "importance": cur["importance"],
                    "tags": cur["tags"], "content": cur["content"],
                    "content_hash": cur["content_hash"],
                    "created_at": cur["created_at"], "updated_at": cur["updated_at"],
                },
                "revisions": [dict(r) for r in rows],
                "revision_count": int(total),
            }
        finally:
            con.close()

    def restore_revision(self, node_id: int, revision: int) -> dict[str, Any]:
        """Restaura una revision archivada escribiendola como version nueva.

        Append-only: la version que sale tambien se archiva, asi que restaurar
        es reversible y el historial no se pierde. Reusa la via unica de
        escritura, de modo que FTS5, entidades y auto-aristas quedan al dia.
        """
        con = self._connect()
        try:
            node = con.execute(
                "SELECT id, topic_key FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not node:
                return {"ok": False, "error": "node not found", "node_id": node_id}
            topic_key = str(node["topic_key"] or "")
            old = con.execute(
                "SELECT revision, content, title, category, source, importance, tags,"
                " content_hash FROM node_revisions WHERE node_id=? AND revision=?",
                (node_id, int(revision)),
            ).fetchone()
        finally:
            con.close()
        if not topic_key:
            return {"ok": False, "node_id": node_id,
                    "error": "el nodo no tiene topic_key: no hay versionado que restaurar"}
        if old is None:
            return {"ok": False, "node_id": node_id,
                    "error": f"revision {revision} no encontrada"}
        try:
            tags = json.loads(old["tags"] or "[]")
        except Exception:
            tags = []
        if not isinstance(tags, list):
            tags = []
        out = self._graph_upsert(
            content=old["content"], topic_key=topic_key,
            category=str(old["category"] or "general"), tags=[str(t) for t in tags],
            title=str(old["title"] or ""), source=str(old["source"] or "restore"),
            importance=int(old["importance"] or 5),
            chash=str(old["content_hash"] or _sha1(str(old["content"] or ""))),
        )
        return {"ok": True, "node_id": node_id, "topic_key": topic_key,
                "restored_from": int(revision), "new_revision": out.get("revision")}

    # ------------------------------------------------------------------
    # maintain (brain_guard-lite)
    # ------------------------------------------------------------------
    def maintain(self) -> dict[str, Any]:
        con = self._connect()
        stats = {"deduped_nodes": 0, "edges_pruned": 0, "self_loops": 0,
                 "entity_edges_added": 0, "rag_chunks_purged": 0}
        try:
            now = _now_iso()
            # 1. self-loops
            cur = con.execute("DELETE FROM edges WHERE from_node=to_node")
            stats["self_loops"] = cur.rowcount or 0
            # 2. dedupe por content_hash (no topic_key): conservar id más alto
            dupes = con.execute(
                """SELECT content_hash, count(*) c FROM nodes
                   WHERE topic_key='' AND content_hash != ''
                   GROUP BY content_hash HAVING c > 1"""
            ).fetchall()
            for d in dupes:
                ids = [int(r["id"]) for r in con.execute(
                    "SELECT id FROM nodes WHERE content_hash=? ORDER BY id",
                    (d["content_hash"],),
                )]
                keep, drop = ids[-1], ids[:-1]
                for old in drop:
                    con.execute("UPDATE OR IGNORE edges SET from_node=? WHERE from_node=?",
                                (keep, old))
                    con.execute("UPDATE OR IGNORE edges SET to_node=? WHERE to_node=?",
                                (keep, old))
                    con.execute("DELETE FROM entity_nodes WHERE node_id=?", (old,))
                    con.execute("DELETE FROM nodes WHERE id=?", (old,))
                    stats["deduped_nodes"] += 1
            con.execute("DELETE FROM edges WHERE from_node=to_node")
            con.execute(
                """UPDATE entities SET mention_count=(
                     SELECT count(*) FROM entity_nodes en
                     WHERE en.entity_lower=entities.name_lower)"""
            )
            con.execute(
                "DELETE FROM entities WHERE mention_count=0"
            )
            # Reconstrucción única de aristas shared_entity para los nodos que
            # existían antes del índice de entidades. Cada nodo conecta con
            # hasta dos vecinos previos por entidad y respeta ambos topes.
            edges_backfill = con.execute(
                "SELECT value FROM knowledge_meta WHERE key='entity_edges_backfilled_v1'"
            ).fetchone()
            if not edges_backfill:
                degrees = {
                    int(r["id"]): int(r["degree"])
                    for r in con.execute(
                        """SELECT n.id,
                                  ((SELECT count(*) FROM edges WHERE from_node=n.id)
                                 + (SELECT count(*) FROM edges WHERE to_node=n.id)) AS degree
                           FROM nodes n"""
                    )
                }
                edge_total = int(con.execute("SELECT count(*) FROM edges").fetchone()[0])
                entity_names = [r["name_lower"] for r in con.execute(
                    "SELECT name_lower FROM entities ORDER BY mention_count DESC, name_lower"
                )]
                for entity in entity_names:
                    node_ids = [int(r[0]) for r in con.execute(
                        "SELECT node_id FROM entity_nodes WHERE entity_lower=? ORDER BY node_id",
                        (entity,),
                    )]
                    for i, node_id in enumerate(node_ids):
                        if edge_total >= MAX_EDGES:
                            break
                        if degrees.get(node_id, 0) >= MAX_EDGES_PER_NODE:
                            continue
                        # Conecta hacia atrás: patrón disperso y determinista,
                        # no clique cuadrático para entidades muy frecuentes.
                        for other in reversed(node_ids[max(0, i - AUTO_LINKS_PER_ENTITY):i]):
                            if degrees.get(node_id, 0) >= MAX_EDGES_PER_NODE:
                                break
                            if degrees.get(other, 0) >= MAX_EDGES_PER_NODE:
                                continue
                            source_id, target_id = sorted((node_id, other))
                            cur = con.execute(
                                """INSERT OR IGNORE INTO edges
                                   (from_node,to_node,relation,weight,note,created_at)
                                   VALUES (?,?,?,?,?,?)""",
                                (source_id, target_id, "shared_entity", 0.5,
                                 f"shared entity: {entity}", now),
                            )
                            if cur.rowcount:
                                degrees[node_id] = degrees.get(node_id, 0) + 1
                                degrees[other] = degrees.get(other, 0) + 1
                                edge_total += 1
                                stats["entity_edges_added"] += 1
                    if edge_total >= MAX_EDGES:
                        break
                con.execute(
                    "INSERT INTO knowledge_meta (key,value) VALUES ('entity_edges_backfilled_v1',?)",
                    (now,),
                )
            # 3. tope MAX_EDGES (podar los de menor weight más antiguos)
            total = con.execute("SELECT count(*) FROM edges").fetchone()[0]
            if total > MAX_EDGES:
                excess = total - MAX_EDGES
                con.execute(
                    """DELETE FROM edges WHERE id IN (
                         SELECT id FROM edges
                         ORDER BY weight ASC, created_at ASC, id ASC LIMIT ?)""",
                    (excess,),
                )
                stats["edges_pruned"] = excess
            # fix #19: purgar chunks RAG de nodos ya no existentes (cascada de dedupe)
            try:
                from src.core import rag as _rag
                node_ids = {int(r[0]) for r in con.execute("SELECT id FROM nodes")}
                rdb = _rag._connect()
                try:
                    orphan_src = []
                    for (s,) in rdb.execute(
                        "SELECT DISTINCT source FROM chunks WHERE source LIKE 'graph:%'"
                    ).fetchall():
                        try:
                            nid = int(s.split(":", 1)[1])
                        except (ValueError, IndexError):
                            continue
                        if nid not in node_ids:
                            orphan_src.append(s)
                    purged = 0
                    for j in range(0, len(orphan_src), 500):
                        batch = orphan_src[j:j + 500]
                        marks = ",".join("?" * len(batch))
                        cur = rdb.execute(
                            f"DELETE FROM chunks WHERE source IN ({marks})", batch
                        )
                        purged += cur.rowcount or 0
                    if purged:
                        rdb.commit()
                    stats["rag_chunks_purged"] = purged
                finally:
                    rdb.close()
            except Exception:
                pass
            con.commit()
            return {"ok": True, **stats,
                    "nodes": con.execute("SELECT count(*) FROM nodes").fetchone()[0],
                    "edges": con.execute("SELECT count(*) FROM edges").fetchone()[0]}
        finally:
            con.close()

    def status(self) -> dict[str, Any]:
        con = self._connect()
        try:
            nodes = con.execute("SELECT count(*) FROM nodes").fetchone()[0]
            edges = con.execute("SELECT count(*) FROM edges").fetchone()[0]
            with_topic = con.execute(
                "SELECT count(*) FROM nodes WHERE topic_key != ''").fetchone()[0]
            revisions = con.execute(
                "SELECT COALESCE(SUM(revision_count-1),0) FROM nodes WHERE revision_count>1"
            ).fetchone()[0]
            archived = con.execute("SELECT count(*) FROM node_revisions").fetchone()[0]
            fts_ok = con.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='nodes_fts'"
            ).fetchone()[0]
            entities = con.execute("SELECT count(*) FROM entities").fetchone()[0]
            connected = con.execute(
                """SELECT count(DISTINCT id) FROM nodes WHERE id IN (
                     SELECT from_node FROM edges UNION SELECT to_node FROM edges)"""
            ).fetchone()[0]
            return {
                "db": str(self.db_path),
                "nodes": nodes, "edges": edges, "connected_nodes": connected,
                "topic_nodes": with_topic, "total_revisions": revisions,
                "archived_revisions": archived,
                "entities": entities, "fts": bool(fts_ok),
                "max_edges": MAX_EDGES, "edges_headroom": max(0, MAX_EDGES - edges),
            }
        finally:
            con.close()

    def touch(self, node_id: int) -> None:
        try:
            con = self._connect()
            try:
                con.execute(
                    "UPDATE nodes SET access_count=access_count+1, last_accessed=? WHERE id=?",
                    (_now_iso(), node_id),
                )
                con.commit()
            finally:
                con.close()
        except Exception:
            pass


def _iso_since(hours: float) -> str:
    ts = time.time() - hours * 3600
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_km: Optional[KnowledgeMemory] = None
_km_lock = threading.Lock()

_hybrid_backends: dict[str, Any] = {}
_hybrid_lock = threading.Lock()


def _hybrid_backend(db_path: Path):
    """Cache one HybridMemoryBackend per brain (avoid re-init per learn call)."""
    key = str(db_path)
    backend = _hybrid_backends.get(key)
    if backend is None:
        from src.core.hybrid_memory import HybridMemoryBackend
        with _hybrid_lock:
            backend = _hybrid_backends.get(key)
            if backend is None:
                backend = HybridMemoryBackend(db_path=db_path)
                _hybrid_backends[key] = backend
    return backend


def get_knowledge_memory() -> KnowledgeMemory:
    global _km
    if _km is None:
        with _km_lock:
            if _km is None:
                _km = KnowledgeMemory()
    return _km


def reset_knowledge_memory() -> None:
    """Solo para tests."""
    global _km
    with _km_lock:
        _km = None
        _SCHEMA_DONE.clear()
        _hybrid_backends.clear()
