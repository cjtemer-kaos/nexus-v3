"""Importador v2 -> v3 SANITIZADO (2026-10-11).

Instruccion del usuario: rellenar los grafos de nexus-v3 con el conocimiento de
supernexus-v2 PERO sin datos personales: IPs, keys/apikeys, contrasenas, ni datos
de sus servidores de Minecraft.

Reglas:
  - EXCLUIR nodos de servidor Minecraft (se borran de v3 los ya copiados y NO se
    importan los de v2).
  - REDACTAR IPs -> [IP]; secretos (sk-*, password=, api_key=, token=, Bearer,
    contrasena) -> [SECRET].
  - Idempotente: dedup por content_hash del contenido YA redactado.

Las rutas NO estan hardcodeadas: salen de --v2/--v3 o de NEXUS_V2_GRAPH /
NEXUS_V3_GRAPH. El grafo de v3 por defecto es el ./brain del repo.

Uso:  python sanitize_and_import.py --v2 <ruta>            (dry-run)
      python sanitize_and_import.py --v2 <ruta> --apply    (aplica, con backup)
"""
from __future__ import annotations
import argparse, hashlib, os, re, shutil, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_V3 = ROOT / "brain" / "knowledge_graph.db"

# --- Minecraft SERVER (no knowledge) ---
MC_SERVER = re.compile(
    r"(LATAMRUST|COBBLEMON|cobbleverse|cobbleverse-sync|MCSS|FabricProxy|pokespawn|"
    r"StaffPanel|Prestige|server\.properties|auth-v1\.5\.1|SERVIDOR[/\\]|/SERVIDOR|"
    r"\bRCON\b|puerto\s*25565|puerto\s*25575|25576|55666|cobbledollars|pokespawnat)",
    re.I)

# --- PII a REDACTAR (no excluir el nodo) ---
RX_IP = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
RX_SK = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
RX_KV = re.compile(r"(?<![A-Za-z0-9])(password|passwd|contrase[nñ]a|api[_-]?key|apikey|auth[_-]?token|"
                   r"access[_-]?token|client[_-]?secret|secret[_-]?key)\b(\s*[:=]\s*)([^\n]{1,300})", re.I)
RX_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.I)


def redact(text: str) -> str:
    if not text:
        return text
    t = RX_SK.sub("[SECRET]", text)
    t = RX_BEARER.sub(r"\1[SECRET]", t)
    t = RX_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[SECRET]", t)
    t = RX_IP.sub("[IP]", t)
    return t


def h(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Importa el grafo de v2 a v3 sanitizado")
    ap.add_argument("--v2", default=os.environ.get("NEXUS_V2_GRAPH", ""),
                    help="knowledge_graph.db de supernexus-v2 (o NEXUS_V2_GRAPH)")
    ap.add_argument("--v3", default=os.environ.get("NEXUS_V3_GRAPH", str(DEFAULT_V3)),
                    help="knowledge_graph.db de nexus-v3 (default: ./brain)")
    ap.add_argument("--apply", action="store_true", help="aplica (default: dry-run)")
    args = ap.parse_args(argv)

    if not args.v2:
        print("Falta el grafo origen: pasá --v2 <ruta> o definí NEXUS_V2_GRAPH.")
        return 2
    v2, v3 = Path(args.v2), Path(args.v3)
    for label, p in (("v2", v2), ("v3", v3)):
        if not p.exists():
            print(f"No existe el grafo {label}: {p}")
            return 2

    c3 = sqlite3.connect(v3)
    c3.row_factory = sqlite3.Row
    c2 = sqlite3.connect(v2)
    c2.row_factory = sqlite3.Row

    # 1) LIMPIAR lo ya copiado en v3 (source=cerebro_migration): borrar MC-server + redactar
    existing = c3.execute("select id, title, content, tags, topic_key from nodes where source='cerebro_migration'").fetchall()
    del_ids, upd = [], 0
    for r in existing:
        blob = (r["title"] or "") + "\n" + (r["content"] or "")
        if MC_SERVER.search(blob):
            del_ids.append(r["id"])
            continue
        new_t, new_c = redact(r["title"]), redact(r["content"])
        new_tags, new_tk = redact(r["tags"]), redact(r["topic_key"])
        if (new_t != r["title"] or new_c != r["content"]
                or new_tags != r["tags"] or new_tk != r["topic_key"]):
            upd += 1
            if args.apply:
                c3.execute("update nodes set title=?, content=?, tags=?, topic_key=?, content_hash=? where id=?",
                           (new_t, new_c, new_tags, new_tk, h(new_c), r["id"]))

    # 2) IMPORTAR de v2 lo que falta (dedup por hash del contenido YA redactado)
    have = set()
    for r in c3.execute("select title, content from nodes").fetchall():
        blob = (r["title"] or "") + "\n" + (r["content"] or "")
        if MC_SERVER.search(blob):
            continue  # se borraran
        have.add(h(redact(r["content"])))
    v2rows = c2.execute("select * from nodes").fetchall()
    skip_mc, dup, ins = 0, 0, 0
    for r in v2rows:
        blob = (r["title"] or "") + "\n" + (r["content"] or "")
        if MC_SERVER.search(blob):
            skip_mc += 1
            continue
        new_t, new_c = redact(r["title"]), redact(r["content"])
        nh = h(new_c)
        if nh in have:
            dup += 1
            continue
        have.add(nh)
        ins += 1
        if args.apply:
            c3.execute(
                "insert into nodes (content,title,category,source,importance,tags,content_hash,"
                "created_at,updated_at,access_count,topic_key,revision_count) "
                "values (?,?,?,?,?,?,?,?,?,0,?,1)",
                (new_c, new_t, r["category"], r["source"], r["importance"], redact(r["tags"]), nh,
                 r["created_at"], r["updated_at"], redact(r["topic_key"])))

    print("=== PLAN v2 -> v3 (sanitizado) ===")
    print(f"  v3 ya-copiados (cerebro_migration): {len(existing)}")
    print(f"    - a BORRAR (Minecraft servidor): {len(del_ids)}")
    print(f"    - a REDACTAR (IP/secretos):      {upd}")
    print(f"  v2 total: {len(v2rows)}")
    print(f"    - EXCLUIDOS (Minecraft servidor): {skip_mc}")
    print(f"    - ya presentes (dedup):           {dup}")
    print(f"    - a IMPORTAR (redactados):        {ins}")
    if args.apply:
        for _id in del_ids:
            c3.execute("delete from nodes where id=?", (_id,))
        c3.commit()
        print(f"\nAPLICADO: {len(del_ids)} borrados, {upd} redactados, {ins} importados.")
    else:
        print("\n(dry-run — usa --apply para aplicar)")
    c3.close(); c2.close()
    return 0


if __name__ == "__main__":
    v2_arg = ""
    for i, a in enumerate(sys.argv):
        if a == "--v2" and i + 1 < len(sys.argv):
            v2_arg = sys.argv[i + 1]
    v2_arg = v2_arg or os.environ.get("NEXUS_V2_GRAPH", "")
    if "--apply" in sys.argv and v2_arg:
        v3_arg = ""
        for i, a in enumerate(sys.argv):
            if a == "--v3" and i + 1 < len(sys.argv):
                v3_arg = sys.argv[i + 1]
        v3_path = Path(v3_arg or os.environ.get("NEXUS_V3_GRAPH", str(DEFAULT_V3)))
        if v3_path.exists():
            bak = v3_path.with_suffix(f".db.bak-sanitize-{time.strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(v3_path, bak)
            print("backup:", bak)
    raise SystemExit(main())
