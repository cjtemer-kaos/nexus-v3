"""Back-port nexus-v3 -> supernexus-v2 (autorizado por el usuario, 2026-10-11).

Copia a supernexus-v2 los nodos de nexus-v3 que NO tiene, sanitizados (sin PII:
IPs, claves, contrasenas, ni datos de servidores Minecraft). Idempotente por hash
del contenido. Backup antes de aplicar.

Uso:  python export_to_v2.py            (dry-run)
      python export_to_v2.py --apply    (aplica, con backup)
"""
from __future__ import annotations
import hashlib, re, shutil, sqlite3, sys, time
from pathlib import Path

V2 = Path(r"D:\ias\proyectos\supernexus-v2\brain\knowledge_graph.db")
V3 = Path(r"D:\ias\proyectos\nexus-v3\brain\knowledge_graph.db")
APPLY = "--apply" in sys.argv

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


def main():
    c2 = sqlite3.connect(V2); c2.row_factory = sqlite3.Row
    c3 = sqlite3.connect(V3); c3.row_factory = sqlite3.Row
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
        if APPLY:
            c2.execute(
                "insert into nodes (content,title,category,source,importance,tags,content_hash,"
                "created_at,updated_at,access_count,topic_key,revision_count) "
                "values (?,?,?,?,?,?,?,?,?,0,?,1)",
                (nc, nt, r["category"], r["source"], r["importance"], redact(r["tags"]), nh,
                 r["created_at"], r["updated_at"], redact(r["topic_key"])))
    print(f"v3 total={len(rows)}  excluidos_MC={skip_mc}  ya_en_v2={dup}  a_importar={ins}")
    if APPLY:
        c2.commit()
        print("APLICADO")
    else:
        print("(dry-run)")
    c2.close(); c3.close()


if __name__ == "__main__":
    if APPLY:
        bak = V2.with_suffix(f".db.bak-fromv3-{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(V2, bak)
        print("backup:", bak)
    main()
