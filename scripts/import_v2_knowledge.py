"""Importador de conocimiento SuperNEXUS v2 -> nexus-v3 (2026-10-26).

Que importa (y por que):

  cerebro.db:conocimientos     -> nodos (topic_key = tema)
  cerebro.db:typed_memories    -> nodos (category = type)
  nexus_memory.db:observations -> nodos (category = category, tags = project)
  knowledge_graph.db:nodes     -> nodos (+ aristas entre nodos mapeados)
  brain/*.md                   -> nodos (findings / decisions / learnings / cloud)

Que NO importa: `memoria` (378 filas tipo "preferencia" con clave gem_code=N, sin
contenido), `conversaciones` (transcripts), `eventos` (telemetria) y las ~250k aristas
del grafo de entidades de v2, que no entran en MAX_EDGES=60000.

Reglas de merge (no pisa lo del usuario):
  1. Contenido ya presente en v3 (texto normalizado) -> se saltea.
  2. Mismo topic_key ya existente y contenido parecido (Jaccard >= 0.6) -> se saltea:
     es el mismo conocimiento, la version de v3 es la vigente.
  3. Mismo topic_key pero contenido distinto (< 0.6) -> entra como nodo aparte con
     topic_key vacio y tag `v2-variante:<tema>`, para no perder nada.
  4. Idempotente: correrlo dos veces no duplica (reglas 1 y 2).

Saneado: excluye el servidor Minecraft; redacta IPs, secretos, tokens y emails.
NO redacta rutas del proyecto (D:\\ias): son la misma maquina y son el valor del nodo.
Si redacta C:\\Users\\<usuario> porque identifica a una persona.

Uso:
    python import_v2_knowledge.py --v2 <brain de v2>              # dry-run
    python import_v2_knowledge.py --v2 <brain de v2> --apply      # aplica (con backup)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_V3 = ROOT / "brain" / "knowledge_graph.db"

MIN_CONTENT = 12          # menos que esto no es conocimiento
SIM_THRESHOLD = 0.6       # Jaccard para considerar "el mismo tema"
MAX_EDGES_DEFAULT = 20000

# --- Servidor Minecraft: no es conocimiento de plataforma ---
MC_SERVER = re.compile(
    r"(LATAMRUST|COBBLEMON|cobbleverse|cobbleverse-sync|MCSS|FabricProxy|pokespawn|"
    r"StaffPanel|Prestige|server\.properties|auth-v1\.5\.1|SERVIDOR[/\\]|/SERVIDOR|"
    r"\bRCON\b|puerto\s*25565|puerto\s*25575|25576|55666|cobbledollars|pokespawnat|"
    r"mcrcon|latamrust-core)",
    re.I)

# --- PII a redactar (no se excluye el nodo) ---
RX_SK = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
RX_GHP = re.compile(r"\b(ghp|gho|ghs)_[A-Za-z0-9]{20,}")
RX_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.I)
RX_KV = re.compile(
    r"(?<![A-Za-z0-9])(password|passwd|contrase[nñ]a|api[_-]?key|apikey|auth[_-]?token|"
    r"access[_-]?token|client[_-]?secret|secret[_-]?key)\b(\s*[:=]\s*)([^\n]{1,300})", re.I)
RX_IP = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
RX_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
RX_USER_PATH = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s\"'`,;)]+\\?[^\s\"'`,;)]*")
RX_HOME = re.compile(r"/home/[A-Za-z0-9_.-]+/[^\s\"'`,;)]*")


def redact(text: str) -> str:
    if not text:
        return text
    t = RX_SK.sub("[SECRET]", text)
    t = RX_GHP.sub("[SECRET]", t)
    t = RX_BEARER.sub(r"\1[SECRET]", t)
    t = RX_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[SECRET]", t)
    t = RX_IP.sub("[IP]", t)
    t = RX_EMAIL.sub("[EMAIL]", t)
    t = RX_USER_PATH.sub("[RUTA]", t)
    t = RX_HOME.sub("[RUTA]", t)
    return t


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_\-]{3,}", (s or "").lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def content_hash(text: str) -> str:
    """Misma convencion que KnowledgeMemory.learn(): sha1 del texto normalizado."""
    return hashlib.sha1((text or "").strip().lower().encode("utf-8", "replace")).hexdigest()


def slug_topic(tema: str) -> str:
    t = re.sub(r"\s+", "-", (tema or "").strip().lower())
    return re.sub(r"[^a-z0-9/_.:-]+", "", t)[:120].strip("-")


def _title_from(content: str, fallback: str = "") -> str:
    line = next((ln.strip() for ln in (content or "").splitlines() if ln.strip()), "")
    line = re.sub(r"^#+\s*", "", line)
    return (line or fallback or "sin titulo")[:120]


def _tags(values) -> str:
    vals = [str(v).strip() for v in (values or []) if str(v or "").strip()]
    return json.dumps(vals, ensure_ascii=False)


# --------------------------------------------------------------------------- fuentes

def from_conocimientos(v2: Path):
    db = v2 / "cerebro.db"
    if not db.exists():
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    for r in con.execute("select tema, contenido, fuente, fecha, utilidad from conocimientos"):
        tema = (r["tema"] or "").strip()
        etiqueta = (r["fuente"] or "direct")[:60]
        yield {
            "content": r["contenido"] or "",
            "title": _title_from(r["contenido"] or "", tema),
            "category": "conocimiento",
            "source": "v2:cerebro",
            "importance": max(1, min(10, int(r["utilidad"] or 5))),
            "tags": [f"v2-fuente:{etiqueta}"],
            "topic_key": slug_topic(tema),
            "created_at": r["fecha"] or "",
            "updated_at": r["fecha"] or "",
        }
    con.close()


def from_typed_memories(v2: Path):
    db = v2 / "cerebro.db"
    if not db.exists():
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    for r in con.execute("select type, title, content, agent_id, confidence, tags, "
                         "created_at, updated_at from typed_memories where status='active'"):
        conf = float(r["confidence"] or 0.5)
        yield {
            "content": r["content"] or "",
            "title": (r["title"] or "")[:120],
            "category": (r["type"] or "memoria")[:30],
            "source": f"v2:typed:{r['agent_id'] or 'desconocido'}"[:60],
            "importance": max(1, min(10, round(conf * 10))),
            "tags": ["v2-types:" + (r["type"] or "")],
            "topic_key": "",
            "created_at": r["created_at"] or "",
            "updated_at": r["updated_at"] or "",
        }
    con.close()


def from_observations(v2: Path):
    db = v2 / "nexus_memory.db"
    if not db.exists():
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    for r in con.execute("select content, category, project, topic_key, agent, ts "
                         "from observations where deleted_at is null"):
        yield {
            "content": r["content"] or "",
            "title": _title_from(r["content"] or "", r["topic_key"] or ""),
            "category": (r["category"] or "observacion")[:40],
            "source": f"v2:obs:{r['agent'] or 'desconocido'}"[:60],
            "importance": 5,
            "tags": [f"v2-proyecto:{r['project']}"] if r["project"] else [],
            "topic_key": slug_topic(r["topic_key"] or ""),
            "created_at": r["ts"] or "",
            "updated_at": r["ts"] or "",
        }
    con.close()


def from_v2_graph(v2: Path):
    db = v2 / "knowledge_graph.db"
    if not db.exists():
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    for r in con.execute("select id, content, title, category, importance, tags, topic_key, "
                         "created_at, updated_at from nodes"):
        try:
            tags = json.loads(r["tags"] or "[]")
        except Exception:
            tags = []
        yield {
            "content": r["content"] or "",
            "title": (r["title"] or "")[:120],
            "category": (r["category"] or "general")[:40],
            "source": "v2:graph",
            "importance": max(1, min(10, int(r["importance"] or 5))),
            "tags": tags if isinstance(tags, list) else [],
            "topic_key": slug_topic(r["topic_key"] or ""),
            "created_at": r["created_at"] or "",
            "updated_at": r["updated_at"] or "",
            "v2_id": r["id"],
        }
    con.close()


def from_markdown(v2: Path):
    for name, cat, imp in (("findings.md", "finding", 6),
                           ("decisions.md", "decision", 7),
                           ("learnings.md", "learning", 6),
                           ("cloud.md", "learning", 5)):
        path = v2 / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for block in re.split(r"(?m)^##\s+", text)[1:]:
            lines = block.splitlines()
            if not lines:
                continue
            head = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            if len(body) < MIN_CONTENT:
                continue
            bold = re.match(r"^\*\*(.+?)\*\*", body.splitlines()[0].strip()) if body else None
            yield {
                "content": f"## {head}\n\n{body}",
                "title": (bold.group(1) if bold else head)[:120],
                "category": cat,
                "source": f"v2:md:{name}",
                "importance": imp,
                "tags": ["v2-md", f"v2-seccion:{head}"[:80]],
                "topic_key": "",
                "created_at": "",
                "updated_at": "",
            }


SOURCES = (
    ("cerebro.conocimientos", from_conocimientos),
    ("cerebro.typed_memories", from_typed_memories),
    ("knowledge_graph.nodes", from_v2_graph),
    ("nexus_memory.observations", from_observations),
    ("brain/*.md", from_markdown),
)


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Importa el conocimiento de v2 al grafo de v3")
    ap.add_argument("--v2", default=os.environ.get("NEXUS_V2_BRAIN", ""),
                    help="carpeta brain de supernexus-v2 (o NEXUS_V2_BRAIN)")
    ap.add_argument("--v3", default=os.environ.get("NEXUS_V3_GRAPH", str(DEFAULT_V3)),
                    help="knowledge_graph.db de nexus-v3 (default: ./brain)")
    ap.add_argument("--apply", action="store_true", help="aplica (default: dry-run)")
    ap.add_argument("--max-edges", type=int, default=MAX_EDGES_DEFAULT,
                    help=f"tope de aristas a importar (default {MAX_EDGES_DEFAULT})")
    args = ap.parse_args(argv)

    if not args.v2:
        print("Falta --v2 <brain de supernexus-v2> o NEXUS_V2_BRAIN.")
        return 2
    v2 = Path(args.v2)
    v3 = Path(args.v3)
    if not v2.is_dir():
        print(f"No existe la carpeta brain de v2: {v2}")
        return 2
    if not v3.exists():
        print(f"No existe el grafo de v3: {v3}")
        return 2

    c3 = sqlite3.connect(v3)
    c3.row_factory = sqlite3.Row
    c3.text_factory = lambda b: b.decode("utf-8", "replace")

    # --- estado actual de v3
    have_text: set[str] = set()
    id_by_text: dict[str, int] = {}
    content_by_topic: dict[str, str] = {}
    id_by_topic: dict[str, int] = {}
    for r in c3.execute("select id, content, topic_key from nodes"):
        nt = norm_text(r["content"])
        have_text.add(nt)
        id_by_text.setdefault(nt, r["id"])
        if r["topic_key"]:
            k = norm_text(r["topic_key"])
            content_by_topic.setdefault(k, r["content"] or "")
            id_by_topic.setdefault(k, r["id"])
    next_id = (c3.execute("select coalesce(max(id), 0) from nodes").fetchone()[0] or 0) + 1
    next_edge = (c3.execute("select coalesce(max(id), 0) from edges").fetchone()[0] or 0) + 1
    edges_before = c3.execute("select count(*) from edges").fetchone()[0]
    nodes_before = c3.execute("select count(*) from nodes").fetchone()[0]
    texts_before = len(have_text)

    stats: dict[str, dict[str, int]] = {}
    pending: list[tuple] = []
    v2_to_v3: dict[int, int] = {}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    for label, fn in SOURCES:
        st = {"total": 0, "mc": 0, "corto": 0, "dup_texto": 0, "dup_tema": 0,
              "variante": 0, "nuevos": 0}
        for cand in fn(v2):
            st["total"] += 1
            raw = cand["content"] or ""
            if len(raw.strip()) < MIN_CONTENT:
                st["corto"] += 1
                continue
            if MC_SERVER.search(raw) or MC_SERVER.search(cand.get("title") or "") \
                    or MC_SERVER.search(cand.get("topic_key") or ""):
                st["mc"] += 1
                continue
            content = redact(raw)
            if len(content.strip()) < MIN_CONTENT:
                st["corto"] += 1
                continue
            nt = norm_text(content)
            if nt in have_text:
                st["dup_texto"] += 1
                if cand.get("v2_id") is not None:
                    v2_to_v3[cand["v2_id"]] = id_by_text[nt]
                continue
            topic = (cand.get("topic_key") or "").strip()
            tags = list(cand.get("tags") or [])
            if topic and norm_text(topic) in content_by_topic:
                prev = content_by_topic[norm_text(topic)]
                sim = jaccard(tokens(content), tokens(prev))
                # Se saltea solo si es el mismo conocimiento y NO aporta mas detalle:
                # la version de v3 manda, salvo que la de v2 sea bastante mas extensa.
                aporta_detalle = len(content) > max(int(len(prev) * 1.5), len(prev) + 400)
                if sim >= SIM_THRESHOLD and not aporta_detalle:
                    st["dup_tema"] += 1
                    if cand.get("v2_id") is not None:
                        v2_to_v3[cand["v2_id"]] = id_by_topic[norm_text(topic)]
                    continue
                st["variante"] += 1
                tags.append(f"v2-variante:{topic}"[:90])
                topic = ""
            new_id = next_id
            next_id += 1
            have_text.add(nt)
            id_by_text.setdefault(nt, new_id)
            if topic:
                content_by_topic[norm_text(topic)] = content
                id_by_topic[norm_text(topic)] = new_id
            if cand.get("v2_id") is not None:
                v2_to_v3[cand["v2_id"]] = new_id
            st["nuevos"] += 1
            pending.append((
                new_id, content, redact((cand.get("title") or "")[:120]) or _title_from(content),
                (cand.get("category") or "general")[:40], (cand.get("source") or "v2")[:60],
                int(cand.get("importance") or 5), _tags([redact(str(t)) for t in tags]),
                content_hash(content), cand.get("created_at") or now,
                cand.get("updated_at") or cand.get("created_at") or now,
                topic or "",
            ))
        stats[label] = st

    # --- aristas del grafo de v2 entre nodos que existen en v3 (importados o ya presentes)
    edge_rows: list[tuple] = []
    v2_graph = v2 / "knowledge_graph.db"
    if v2_graph.exists() and v2_to_v3:
        c2 = sqlite3.connect(f"file:{v2_graph}?mode=ro", uri=True)
        c2.row_factory = sqlite3.Row
        seen: set[tuple[int, int, str]] = set()
        for r in c2.execute("select from_node, to_node, relation, weight, note, created_at "
                            "from edges order by weight desc"):
            a, b = v2_to_v3.get(r["from_node"]), v2_to_v3.get(r["to_node"])
            if not a or not b or a == b:
                continue
            key = (a, b, r["relation"] or "relates_to")
            if key in seen:
                continue
            seen.add(key)
            edge_rows.append((next_edge, a, b, (r["relation"] or "relates_to")[:40],
                              float(r["weight"] or 0.5), redact((r["note"] or "")[:200]),
                              r["created_at"] or now))
            next_edge += 1
            if len(edge_rows) >= max(1, args.max_edges):
                break
        c2.close()

    # --- reporte
    print("=== IMPORT v2 -> v3 ===")
    print(f"  origen: {v2}")
    print(f"  destino: {v3}")
    print(f"  v3 antes: {nodes_before} nodos | {texts_before} textos unicos | "
          f"aristas {edges_before}")
    print()
    print(f"  {'fuente':<26}{'total':>7}{'MC':>6}{'corto':>7}{'dup':>7}{'dupTema':>9}"
          f"{'variante':>10}{'nuevos':>8}")
    tot = {k: 0 for k in ("total", "mc", "corto", "dup_texto", "dup_tema", "variante", "nuevos")}
    for label, st in stats.items():
        print(f"  {label:<26}{st['total']:>7}{st['mc']:>6}{st['corto']:>7}{st['dup_texto']:>7}"
              f"{st['dup_tema']:>9}{st['variante']:>10}{st['nuevos']:>8}")
        for k in tot:
            tot[k] += st[k]
    print(f"  {'TOTAL':<26}{tot['total']:>7}{tot['mc']:>6}{tot['corto']:>7}{tot['dup_texto']:>7}"
          f"{tot['dup_tema']:>9}{tot['variante']:>10}{tot['nuevos']:>8}")
    print()
    print(f"  nodos a insertar: {len(pending)}")
    print(f"  aristas a insertar: {len(edge_rows)} (tope {args.max_edges})")
    print(f"  v3 despues: ~{nodes_before + len(pending)} nodos | "
          f"{len(have_text)} textos unicos | aristas ~{edges_before + len(edge_rows)}"
          f" (max 60000)")

    if not args.apply:
        print("\n(dry-run: usa --apply para aplicar)")
        c3.close()
        return 0

    # --- backup + escritura
    bak = v3.with_name(f"{v3.stem}.pre-import-{time.strftime('%Y%m%d-%H%M%S')}.db")
    c3.execute("PRAGMA wal_checkpoint(FULL)")
    c3.close()
    shutil.copy2(v3, bak)
    print(f"\n  backup: {bak}")

    c3 = sqlite3.connect(v3)
    try:
        c3.executemany(
            "insert into nodes (id, content, title, category, source, importance, tags,"
            " content_hash, created_at, updated_at, access_count, last_accessed,"
            " topic_key, revision_count) values (?,?,?,?,?,?,?,?,?,?,0,NULL,?,1)",
            pending)
        c3.executemany(
            "insert or ignore into edges (id, from_node, to_node, relation, weight, note,"
            " created_at) values (?,?,?,?,?,?,?)",
            edge_rows)
        c3.commit()
    except Exception as exc:
        c3.rollback()
        c3.close()
        print(f"\n  ERROR: {exc}\n  rollback hecho; restaura el backup si hace falta.")
        return 1

    print(f"  APLICADO: {len(pending)} nodos, {len(edge_rows)} aristas intentadas")
    print(f"  v3 ahora: {c3.execute('select count(*) from nodes').fetchone()[0]} nodos | "
          f"{c3.execute('select count(*) from edges').fetchone()[0]} aristas")
    c3.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
