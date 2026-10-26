"""Back-port nexus-v3 -> supernexus-v2 (autorizado por el usuario, 2026-10-11).

Copia a supernexus-v2 los nodos de nexus-v3 que NO tiene, sanitizados (sin PII:
IPs, claves, contrasenas, ni datos de servidores Minecraft). Idempotente por hash
del contenido. Backup antes de aplicar.

Las rutas NO estan hardcodeadas: salen de --v2/--v3 o de NEXUS_V2_GRAPH /
NEXUS_V3_GRAPH. El grafo de v3 por defecto es el ./brain del repo.

Uso:  python export_to_v2.py --v2 <ruta knowledge_graph.db>            (dry-run)
      python export_to_v2.py --v2 <ruta> --apply                       (aplica)
"""
from __future__ import annotations
import argparse, hashlib, os, re, shutil, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_V3 = ROOT / "brain" / "knowledge_graph.db"

MC_SERVER = re.compile(
    r"(LATAMRUST|COBBLEMON|cobblemon|cobbleverse|MCSS|FabricProxy|pokespawn|StaffPanel|"
    r"Prestige|server\.properties|\bRCON\b|cobbledollars|biolith|latamrust-core|"
    r"hermes-discord\.properties|SERVIDOR[/\\]|/SERVIDOR|velocity)", re.I)
RX_IP = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
RX_SK = re.compile(r"\bsk-[A-Za-z0-9_\-.]{4,}")
RX_KV = re.compile(r"(?<![A-Za-z0-9])(password|passwd|contrase[nñ]a|api[_-]?key|apikey|auth[_-]?token|"
                   r"access[_-]?token|client[_-]?secret|secret[_-]?key)\b(\s*[:=]\s*)([^\n]{1,300})", re.I)
RX_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.I)


def redact(t: str) -> str:
    if not t:
        return t
    t = RX_SK.sub("[SECRET]", t)
    t = RX_BEARER.sub(r"\1[SECRET]", t)
    t = RX_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[SECRET]", t)
    t = RX_IP.sub("[IP]", t)
    return t


def h(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Back-port de nexus-v3 a supernexus-v2")
    ap.add_argument("--v2", default=os.environ.get("NEXUS_V2_GRAPH", ""),
                    help="knowledge_graph.db de supernexus-v2 (o NEXUS_V2_GRAPH)")
    ap.add_argument("--v3", default=os.environ.get("NEXUS_V3_GRAPH", str(DEFAULT_V3)),
                    help="knowledge_graph.db de nexus-v3 (default: ./brain)")
    ap.add_argument("--apply", action="store_true", help="aplica (default: dry-run)")
    args = ap.parse_args(argv)

    if not args.v2:
        print("Falta el grafo destino: pasá --v2 <ruta> o definí NEXUS_V2_GRAPH.")
        print("Ejemplo: python export_to_v2.py --v2 C:\\ruta\\supernexus-v2\\brain\\knowledge_graph.db")
        return 2
    v2, v3 = Path(args.v2), Path(args.v3)
    for label, p in (("v2", v2), ("v3", v3)):
        if not p.exists():
            print(f"No existe el grafo {label}: {p}")
            return 2

    c2 = sqlite3.connect(v2); c2.row_factory = sqlite3.Row
    c3 = sqlite3.connect(v3); c3.row_factory = sqlite3.Row
    have2 = set(h(redact(r["content"])) for r in c2.execute("select content from nodes"))
    rows = c3.execute("select * from nodes").fetchall()
    skip_mc = dup = ins = 0
    for r in rows:
        blob = (r["title"] or "") + "\n" + (r["content"] or "")
        if MC_SERVER.search(blob):
            skip_mc += 1
            continue
        nt, nc = redact(r["title"]), redact(r["content"])
        nh = h(nc)
        if nh in have2:
            dup += 1
            continue
        have2.add(nh)
        ins += 1
        if args.apply:
            c2.execute(
                "insert into nodes (content,title,category,source,importance,tags,content_hash,"
                "created_at,updated_at,access_count,topic_key,revision_count) "
                "values (?,?,?,?,?,?,?,?,?,0,?,1)",
                (nc, nt, r["category"], r["source"], r["importance"], redact(r["tags"]), nh,
                 r["created_at"], r["updated_at"], redact(r["topic_key"])))
    print(f"v3 total={len(rows)}  excluidos_MC={skip_mc}  ya_en_v2={dup}  a_importar={ins}")
    if args.apply:
        c2.commit()
        print("APLICADO")
    else:
        print("(dry-run)")
    c2.close(); c3.close()
    return 0


if __name__ == "__main__":
    if "--apply" in sys.argv:
        v2_arg = ""
        for i, a in enumerate(sys.argv):
            if a == "--v2" and i + 1 < len(sys.argv):
                v2_arg = sys.argv[i + 1]
        v2_arg = v2_arg or os.environ.get("NEXUS_V2_GRAPH", "")
        if v2_arg and Path(v2_arg).exists():
            bak = Path(v2_arg).with_suffix(f".db.bak-fromv3-{time.strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(v2_arg, bak)
            print("backup:", bak)
    raise SystemExit(main())
