"""Bundles portables de conocimiento (grafo OPCODE).

Formato `.nexuskg.json`:
- nodos con una clave estable (`topic_key` o `content_hash`), sin ids locales;
- aristas referenciando esas claves;
- manifiesto con version, conteos y hash canonico.

Importar es merge por clave: no duplica, archiva revision al actualizar un
`topic_key` y omite nodos ya presentes. El modo por defecto es dry-run.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "nexus.knowledge.bundle/v1"
MAX_NODES = 50_000
MAX_EDGES = 100_000
MAX_CONTENT_CHARS = 200_000


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha1(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode("utf-8")).hexdigest()


def _canonical_hash(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    payload = json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("nexus-v3")
    except Exception:
        return "unknown"


def _tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def _node_key(node: dict[str, Any]) -> str:
    key = (node.get("topic_key") or "").strip()
    if key:
        return key
    chash = (node.get("content_hash") or "").strip()
    if chash:
        return chash
    return "content:" + _sha1(str(node.get("content") or ""))


def export_bundle(km, *, categories: Iterable[str] | None = None,
                  include_sessions: bool = False) -> dict[str, Any]:
    """Exporta nodos/aristas del grafo a un bundle portable."""
    cats = [str(c).strip() for c in (categories or []) if str(c).strip()]
    where: list[str] = []
    params: list[Any] = []
    if not include_sessions:
        where.append("category != 'session'")
    if cats:
        where.append("category IN (" + ",".join("?" for _ in cats) + ")")
        params.extend(cats)
    sql = ("SELECT id, content, title, category, source, importance, tags, topic_key, "
           "content_hash, created_at, updated_at, revision_count FROM nodes")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    con = km._connect()
    try:
        rows = [dict(r) for r in con.execute(sql, params)]
        ids = {int(r["id"]) for r in rows}
        edge_rows = []
        if ids:
            edge_rows = [dict(r) for r in con.execute(
                "SELECT from_node, to_node, relation, weight, note, created_at FROM edges "
                "ORDER BY id")]
    finally:
        con.close()

    nodes: list[dict[str, Any]] = []
    id_to_key: dict[int, str] = {}
    used: set[str] = set()
    for r in rows:
        key = _node_key(r)
        if key in used:
            key = f"{key}#{r['id']}"
        used.add(key)
        id_to_key[int(r["id"])] = key
        nodes.append({
            "key": key,
            "topic_key": r.get("topic_key") or "",
            "content": r.get("content") or "",
            "title": r.get("title") or "",
            "category": r.get("category") or "general",
            "source": r.get("source") or "nexus",
            "importance": int(r.get("importance") or 5),
            "tags": _tags(r.get("tags")),
            "content_hash": r.get("content_hash") or "",
            "created_at": r.get("created_at") or "",
            "updated_at": r.get("updated_at") or "",
            "revision_count": int(r.get("revision_count") or 0),
        })

    edges: list[dict[str, Any]] = []
    for e in edge_rows:
        frm = id_to_key.get(int(e["from_node"]))
        to = id_to_key.get(int(e["to_node"]))
        if not frm or not to:
            continue
        edges.append({
            "from": frm, "to": to,
            "relation": e.get("relation") or "relates_to",
            "weight": float(e.get("weight") or 1.0),
            "note": e.get("note") or "",
            "created_at": e.get("created_at") or "",
        })

    bundle = {
        "schema": SCHEMA,
        "created_at": _now_iso(),
        "source": {"app": "nexus-v3", "version": _version()},
        "counts": {"nodes": len(nodes), "edges": len(edges)},
        "nodes": nodes,
        "edges": edges,
        "content_hash": _canonical_hash(nodes, edges),
    }
    return bundle


def validate_bundle(bundle: dict[str, Any]) -> None:
    if not isinstance(bundle, dict):
        raise ValueError("bundle debe ser un objeto JSON")
    if bundle.get("schema") != SCHEMA:
        raise ValueError(f"schema no soportado: {bundle.get('schema')!r} (esperado {SCHEMA})")
    nodes = bundle.get("nodes")
    edges = bundle.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("nodes/edges deben ser listas")
    if len(nodes) > MAX_NODES:
        raise ValueError(f"bundle excede {MAX_NODES} nodos")
    if len(edges) > MAX_EDGES:
        raise ValueError(f"bundle excede {MAX_EDGES} aristas")
    for n in nodes:
        if not isinstance(n, dict) or not str(n.get("content") or "").strip():
            raise ValueError("cada nodo necesita content no vacio")
        if len(str(n.get("content") or "")) > MAX_CONTENT_CHARS:
            raise ValueError(f"nodo excede {MAX_CONTENT_CHARS} caracteres")
        if not str(n.get("key") or n.get("topic_key") or "").strip() and not str(n.get("content_hash") or "").strip():
            n["key"] = "content:" + _sha1(str(n.get("content") or ""))
    for e in edges:
        if not isinstance(e, dict) or not str(e.get("from") or "").strip() or not str(e.get("to") or "").strip():
            raise ValueError("cada arista necesita from/to")
    expected = bundle.get("content_hash")
    if expected:
        got = _canonical_hash(nodes, edges)
        if got != expected:
            raise ValueError("content_hash no coincide: bundle corrupto o editado")


def _load_index(km) -> dict[str, dict[str, dict[str, Any]]]:
    con = km._connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, topic_key, content_hash, updated_at FROM nodes")]
    finally:
        con.close()
    by_topic: dict[str, dict[str, Any]] = {}
    by_hash: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("topic_key"):
            by_topic.setdefault(str(r["topic_key"]), r)
        if r.get("content_hash"):
            by_hash.setdefault(str(r["content_hash"]), r)
    return {"by_topic": by_topic, "by_hash": by_hash}


def _existing(km, node: dict[str, Any],
              index: dict[str, dict[str, dict[str, Any]]] | None = None
              ) -> dict[str, Any] | None:
    topic_key = str(node.get("topic_key") or "").strip()
    content = str(node.get("content") or "")
    declared = str(node.get("content_hash") or "").strip()
    calc = _sha1(content)
    if index is not None:
        if topic_key and topic_key in index["by_topic"]:
            return index["by_topic"][topic_key]
        for chash in (declared, calc):
            if chash and chash in index["by_hash"]:
                return index["by_hash"][chash]
    con = km._connect()
    try:
        if topic_key:
            row = con.execute(
                "SELECT id, content_hash, updated_at FROM nodes WHERE topic_key=? LIMIT 1",
                (topic_key,)).fetchone()
            return dict(row) if row else None
        # El hash historico de un nodo puede no coincidir con el sha1 actual
        # del contenido. Probar el declarado (indexado) y despues el calculado
        # evita duplicar al reimportar el mismo grafo.
        for chash in (declared, calc):
            if not chash:
                continue
            row = con.execute(
                "SELECT id, content_hash, updated_at FROM nodes "
                "WHERE content_hash=? ORDER BY id LIMIT 1", (chash,)).fetchone()
            if row:
                return dict(row)
        row = con.execute(
            "SELECT id, content_hash, updated_at FROM nodes "
            "WHERE content=? ORDER BY id LIMIT 1", (content,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _node_content(km, node_id: int) -> str:
    con = km._connect()
    try:
        row = con.execute("SELECT content FROM nodes WHERE id=?", (node_id,)).fetchone()
        return str(row[0]) if row else ""
    finally:
        con.close()


def _load_edge_index(km) -> set[tuple[int, int, str]]:
    con = km._connect()
    try:
        return {(int(r[0]), int(r[1]), str(r[2])) for r in con.execute(
            "SELECT from_node, to_node, relation FROM edges")}
    finally:
        con.close()


def import_bundle(km, bundle: dict[str, Any], *, dry_run: bool = True,
                  overwrite: bool = False, source_label: str = "bundle") -> dict[str, Any]:
    """Mergea un bundle en el grafo local. Dry-run por defecto."""
    validate_bundle(bundle)
    nodes = bundle.get("nodes", [])
    edges = bundle.get("edges", [])
    counts = {"new": 0, "updated": 0, "skipped": 0, "total": len(nodes)}
    edge_counts = {"new": 0, "skipped": 0, "total": len(edges)}
    warnings: list[str] = []
    key_to_id: dict[str, int] = {}
    new_keys: set[str] = set()
    index = _load_index(km)

    def remember(topic_key: str, out: dict[str, Any], chash: str,
                 updated_at: str) -> None:
        entry = {"id": int(out["node_id"]), "content_hash": chash,
                 "updated_at": updated_at}
        if topic_key:
            index["by_topic"][topic_key] = entry
        index["by_hash"][chash] = entry

    for node in nodes:
        key = str(node.get("key") or _node_key(node))
        content = str(node.get("content") or "")
        declared = str(node.get("content_hash") or "").strip()
        # El contenido manda, pero el hash declarado sirve para reconocer
        # nodos historicos cuyo content_hash no es el sha1 actual.
        chash = _sha1(content) or declared
        old = _existing(km, node, index)
        if old is not None:
            key_to_id[key] = int(old["id"])
            same = (old.get("content_hash") or "") == chash
            if not same:
                # El hash declarado puede ser historico; el contenido manda.
                same = _node_content(km, int(old["id"])) == content
            newer = str(node.get("updated_at") or "") > str(old.get("updated_at") or "")
            if same or (not overwrite and not newer):
                counts["skipped"] += 1
                continue
            topic_key = str(node.get("topic_key") or "").strip()
            if not topic_key:
                # Sin clave estable no se puede actualizar sin duplicar.
                counts["skipped"] += 1
                continue
            counts["updated"] += 1
            if dry_run:
                continue
            out = km.learn(
                content, topic_key=topic_key,
                category=str(node.get("category") or "general"),
                tags=_tags(node.get("tags")), title=str(node.get("title") or ""),
                source=str(node.get("source") or f"import:{source_label}"),
                importance=max(1, min(10, int(node.get("importance") or 5))),
                index_rag=False, persist_markdown=False, persist_observation=False,
            )
            key_to_id[key] = int(out["node_id"])
            remember(topic_key, out, chash, str(node.get("updated_at") or ""))
        else:
            counts["new"] += 1
            new_keys.add(key)
            if dry_run:
                # id sintetico para poder simular las aristas del bundle
                key_to_id[key] = -(len(key_to_id) + 1)
                continue
            topic_key = str(node.get("topic_key") or "").strip()
            out = km.learn(
                content, topic_key=topic_key,
                category=str(node.get("category") or "general"),
                tags=_tags(node.get("tags")), title=str(node.get("title") or ""),
                source=str(node.get("source") or f"import:{source_label}"),
                importance=max(1, min(10, int(node.get("importance") or 5))),
                index_rag=False, persist_markdown=False, persist_observation=False,
            )
            key_to_id[key] = int(out["node_id"])
            remember(topic_key, out, chash, str(node.get("updated_at") or ""))

    edge_index = _load_edge_index(km)

    for edge in edges:
        frm_key = str(edge.get("from") or "")
        to_key = str(edge.get("to") or "")
        relation = str(edge.get("relation") or "relates_to").strip().lower() or "relates_to"
        if frm_key not in key_to_id or to_key not in key_to_id:
            edge_counts["skipped"] += 1
            if len(warnings) < 20:
                warnings.append(f"arista sin nodo: {frm_key} -> {to_key}")
            continue
        frm_id, to_id = key_to_id[frm_key], key_to_id[to_key]
        if frm_id == to_id:
            edge_counts["skipped"] += 1
            continue
        if frm_key in new_keys or to_key in new_keys:
            edge_counts["new"] += 1
            edge_index.add((frm_id, to_id, relation))
            if dry_run:
                continue
        elif (frm_id, to_id, relation) in edge_index:
            edge_counts["skipped"] += 1
            continue
        else:
            edge_counts["new"] += 1
            edge_index.add((frm_id, to_id, relation))
            if dry_run:
                continue
        res = km.add_relation(
            frm_id, to_id, relation,
            weight=max(0.0, min(1.0, float(edge.get("weight") or 1.0))),
            note=str(edge.get("note") or "")[:2000],
        )
        if not res.get("added"):
            edge_counts["new"] -= 1
            edge_counts["skipped"] += 1
            if len(warnings) < 20:
                warnings.append(f"arista omitida ({res.get('reason')}): {frm_key} -> {to_key}")

    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "schema": SCHEMA,
        "content_hash": bundle.get("content_hash") or "",
        "source": bundle.get("source") or {},
        "nodes": counts,
        "edges": edge_counts,
        "warnings": warnings,
    }


def write_bundle(bundle: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def read_bundle(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_bundle(data)
    return data
