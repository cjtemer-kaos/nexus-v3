"""Exporta la BASE de conocimiento de nexus-v3 a un bundle versionado.

La idea: el conocimiento que ya tiene el grafo (arquitectura, lecciones, fixes,
decisiones, procedimientos) es parte del producto, no un dato de una PC. Se
exporta saneado y se versiona en `brain/seed/` para que TODA instalacion nueva
arranque con la misma base; despues cada usuario sigue agregando lo suyo.

Que hace:
  - exporta nodos/aristas del grafo local (via knowledge_bundle.export_bundle)
  - EXCLUYE lo que es de un proyecto/PC puntual (servidor Minecraft)
  - REDACT a secretos, IPs, emails y rutas absolutas de esta maquina
  - fusiona la semilla curada del sistema (make_seed_bundle) para que la base
    documente siempre la instalacion/arquitectura/MCP aunque el grafo no la tenga
  - escribe `brain/seed/nexus-base.nexuskg.json.gz` (gzip: el JSON pesa ~15 MB)

Uso:
    python scripts/make_base_bundle.py                # escribe la base
    python scripts/make_base_bundle.py --json         # solo estadisticas
    python scripts/make_base_bundle.py --out otra.ruta.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.core.knowledge_bundle import SCHEMA, _canonical_hash, _sha1, export_bundle  # noqa: E402

OUT = ROOT / "brain" / "seed" / "nexus-base.nexuskg.json.gz"

# --- Excluir: datos de un proyecto/PC concreta (no son base del producto) ----
EXCLUDE = re.compile(
    r"(LATAMRUST|COBBLEMON|cobbleverse|MCSS|FabricProxy|pokespawn|StaffPanel|"
    r"Prestige|server\.properties|latamrust-core|cobbledollars|"
    r"hermes-discord\.properties|minecraft_rcon|mcrcon|mc_manager|/api/mc/)", re.I)

# --- Redactar: secretos y datos de la maquina ------------------------------
RX_SECRET = re.compile(
    r"\bsk-[A-Za-z0-9_\-]{8,}"
    r"|\bghp_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[baprs]-[A-Za-z0-9\-]{8,}"
    r"|\bAIza[0-9A-Za-z_\-]{20,}"
    r"|\b[A-Za-z0-9_-]{24,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,38}\b")
RX_KV = re.compile(
    r"(?i)(?<![A-Za-z0-9])(password|passwd|contrasena|contraseña|api[_-]?key|apikey|"
    r"auth[_-]?token|access[_-]?token|client[_-]?secret|secret[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;\"']{4,})")
RX_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")
RX_IP = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
RX_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
RX_PATHS = [
    (re.compile(r"(?i)D:\\ias\\proyectos\\nexus-v3"), "<repo>"),
    (re.compile(r"(?i)D:\\ias"), "<ias>"),
    (re.compile(r"(?i)C:\\Users\\cjtr"), r"C:\Users\<usuario>"),
    (re.compile(r"/home/(?!\<)[a-z][a-z0-9_-]{2,}"), "/home/<usuario>"),
]


def sanitize(text: str) -> tuple[str, bool]:
    if not text:
        return text, False
    before = text
    text = RX_SECRET.sub("[SECRET]", text)
    text = RX_BEARER.sub(r"\1[SECRET]", text)
    text = RX_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[SECRET]", text)
    text = RX_IP.sub("[IP]", text)
    text = RX_EMAIL.sub("[EMAIL]", text)
    for rx, repl in RX_PATHS:
        text = rx.sub(repl.replace("\\", "\\\\"), text)
    return text, text != before


def _tags(value) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,;]", value) if p.strip()]
    return []


def curated_nodes() -> tuple[list[dict], list[dict]]:
    """Semilla curada del sistema, reusada para que la base siempre la tenga."""
    import importlib.util

    path = ROOT / "scripts" / "make_seed_bundle.py"
    spec = importlib.util.spec_from_file_location("_make_seed_bundle", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    nodes = []
    for name, topic, title, content, tags, importance in mod.NODES:
        nodes.append({
            "key": topic, "topic_key": topic, "content": content, "title": title,
            "category": "nexus", "source": "seed:nexus-v3", "importance": importance,
            "tags": tags, "content_hash": _sha1(content),
            "created_at": "2026-10-26T00:00:00Z", "updated_at": "2026-10-26T00:00:00Z",
            "revision_count": 1,
        })
    edges = [{"from": f, "to": t, "relation": r, "weight": 1.0, "note": "",
              "created_at": "2026-10-26T00:00:00Z"} for f, t, r in mod.EDGES]
    return nodes, edges


def build(graph: str = "") -> tuple[dict, dict]:
    from src.core.knowledge_memory import KnowledgeMemory

    km = KnowledgeMemory(Path(graph)) if graph else KnowledgeMemory()
    raw = export_bundle(km)

    stats = {"graph": str(km.db_path), "exported_nodes": len(raw["nodes"]),
             "exported_edges": len(raw["edges"]), "excluded": 0, "redacted": 0,
             "kept": 0}
    nodes, kept_keys = [], set()
    for n in raw["nodes"]:
        blob = (n.get("title") or "") + "\n" + (n.get("content") or "")
        if EXCLUDE.search(blob):
            stats["excluded"] += 1
            continue
        content, ch1 = sanitize(n.get("content") or "")
        title, ch2 = sanitize(n.get("title") or "")
        source, ch3 = sanitize(str(n.get("source") or ""))
        if ch1 or ch2 or ch3:
            stats["redacted"] += 1
        node = dict(n)
        node["content"], node["title"], node["source"] = content, title, source
        node["tags"] = [sanitize(t)[0] for t in _tags(n.get("tags"))]
        node["content_hash"] = _sha1(content)
        nodes.append(node)
        kept_keys.add(node["key"])

    edges = [e for e in raw["edges"] if e["from"] in kept_keys and e["to"] in kept_keys]

    # Fusionar la semilla curada (no duplica: mismo key/topic_key).
    extra_nodes, extra_edges = curated_nodes()
    have_keys = {n["key"] for n in nodes}
    have_topics = {n.get("topic_key") for n in nodes if n.get("topic_key")}
    for n in extra_nodes:
        if n["key"] not in have_keys and n["topic_key"] not in have_topics:
            nodes.append(n)
            kept_keys.add(n["key"])
    for e in extra_edges:
        if e["from"] in kept_keys and e["to"] in kept_keys:
            edges.append(e)

    bundle = {
        "schema": SCHEMA,
        "created_at": "2026-10-26T00:00:00Z",
        "source": {"app": "nexus-v3", "version": "base"},
        "counts": {"nodes": len(nodes), "edges": len(edges)},
        "nodes": nodes,
        "edges": edges,
        "content_hash": _canonical_hash(nodes, edges),
    }
    stats.update(kept=len(nodes), kept_edges=len(edges),
                 curated_added=len(nodes) - (stats["exported_nodes"] - stats["excluded"]))
    return bundle, stats


def write_gz(bundle: dict, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # mtime=0: el archivo gz es reproducible byte a byte (no ensucia el diff).
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                           compresslevel=9, mtime=0) as fh:
            fh.write(payload)
    return path.stat().st_size


def main() -> int:
    ap = argparse.ArgumentParser(description="Genera la base de conocimiento portable")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--graph", default="", help="knowledge_graph.db alternativo")
    ap.add_argument("--json", action="store_true", help="solo estadisticas")
    args = ap.parse_args()

    bundle, stats = build(args.graph)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    out = Path(args.out)
    size = write_gz(bundle, out)
    print(f"grafo origen: {stats['graph']}")
    print(f"exportados:   {stats['exported_nodes']} nodos / {stats['exported_edges']} aristas")
    print(f"excluidos:    {stats['excluded']} (datos de proyecto/PC)")
    print(f"redactados:   {stats['redacted']} (secretos, IPs, emails, rutas)")
    print(f"base final:   {stats['kept']} nodos / {stats['kept_edges']} aristas")
    print(f"escrito:      {out}  ({size / 1024 / 1024:.1f} MB gz)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
