"""Crea el grafo de nexus-v3 e importa la base de conocimiento compartida.

El grafo de nexus-v3 no arranca vacio: `brain/seed/` viaja con el producto y
trae la base de conocimiento (arquitectura, lecciones, fixes, decisiones,
MCP) que hace utiles a `recall`, al RAG y a las tools desde el primer arranque.

Todos los archivos `brain/seed/*.nexuskg.json` y `*.nexuskg.json.gz` se
importan SIEMPRE, con merge por `topic_key` y `overwrite=False`:

  - instalacion nueva  -> copia la base completa;
  - instalacion usada  -> suma los nodos base que falten, sin pisar lo que el
    usuario ya aprendio (el usuario sigue creando su propio conocimiento).

Uso:
    python scripts/bootstrap_brain.py              # importa toda la base
    python scripts/bootstrap_brain.py --dry-run    # que haria, sin escribir
    python scripts/bootstrap_brain.py --if-empty   # solo si el grafo esta vacio
    python scripts/bootstrap_brain.py --seed otro.nexuskg.json
    python scripts/bootstrap_brain.py --json
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SEED_DIR = ROOT / "brain" / "seed"
SEED_SUFFIXES = (".nexuskg.json", ".nexuskg.json.gz")


def list_seeds(directory: Path) -> list[Path]:
    """Todos los bundles de la carpeta, ordenados (la base primero)."""
    if not directory.is_dir():
        return []
    found = [p for p in directory.iterdir()
             if p.is_file() and p.name.endswith(SEED_SUFFIXES)]
    return sorted(found, key=lambda p: p.name)


def read_seed(path: Path) -> dict:
    if path.name.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    return data


def seed_marker(km, name: str) -> str:
    """Hash del ultimo bundle importado con ese nombre ("" si nunca)."""
    try:
        con = km._connect()
        try:
            row = con.execute("SELECT value FROM knowledge_meta WHERE key=?",
                              (f"seed:{name}",)).fetchone()
            return str(row["value"]) if row else ""
        finally:
            con.close()
    except Exception:
        return ""


def set_seed_marker(km, name: str, value: str) -> None:
    try:
        con = km._connect()
        try:
            con.execute("INSERT OR REPLACE INTO knowledge_meta (key,value) VALUES (?,?)",
                        (f"seed:{name}", value))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Grafo + base de conocimiento de nexus-v3")
    ap.add_argument("--seed", default="",
                    help="importa solo este bundle (.nexuskg.json o .json.gz)")
    ap.add_argument("--if-empty", action="store_true",
                    help="no importa si el grafo ya tiene nodos")
    ap.add_argument("--dry-run", action="store_true",
                    help="calcula el resultado sin escribir en el grafo")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from src.brain.link import brain_path
    from src.core.knowledge_bundle import import_bundle
    from src.core.knowledge_memory import KnowledgeMemory

    seeds = [Path(args.seed)] if args.seed else list_seeds(SEED_DIR)

    km = KnowledgeMemory()
    status = km.status() if hasattr(km, "status") else {}
    before = int(status.get("nodes") or 0)
    result: dict = {"brain_dir": str(brain_path()), "db": str(km.db_path),
                    "nodes_before": before, "nodes_after": before,
                    "dry_run": bool(args.dry_run), "seeds": [],
                    "imported": {"new": 0, "updated": 0, "skipped": 0}}

    if not seeds:
        result["warning"] = f"sin bundles en {SEED_DIR}"
    elif args.if_empty and before > 0:
        result["skipped"] = f"el grafo ya tiene {before} nodos (--if-empty)"
    else:
        for seed in seeds:
            item: dict = {"file": str(seed), "bytes": seed.stat().st_size
                          if seed.exists() else 0}
            if not seed.exists():
                item["error"] = "no existe"
                result["seeds"].append(item)
                continue
            try:
                bundle = read_seed(seed)
            except Exception as exc:  # bundle corrupto: no abortar el resto
                item["error"] = f"{type(exc).__name__}: {exc}"
                result["seeds"].append(item)
                continue
            chash = str(bundle.get("content_hash") or "")
            if chash and not args.dry_run and seed_marker(km, seed.name) == chash:
                item["already"] = True
                item.update({"new": 0, "updated": 0, "skipped": len(bundle.get("nodes") or []),
                             "total": len(bundle.get("nodes") or []), "edges": 0})
                result["seeds"].append(item)
                continue
            if not args.json:
                print(f"Importando {seed.name} "
                      f"({len(bundle.get('nodes') or [])} nodos, puede tardar unos minutos)...")
            rep = import_bundle(km, bundle, dry_run=bool(args.dry_run),
                                overwrite=False, source_label=seed.name)
            nodes_rep = rep.get("nodes") or {}
            edges_rep = rep.get("edges") or {}
            item.update({k: int(nodes_rep.get(k) or 0)
                         for k in ("new", "updated", "skipped", "total")})
            item["edges"] = int(edges_rep.get("new") or 0)
            result["seeds"].append(item)
            for key in ("new", "updated", "skipped"):
                result["imported"][key] += int(item.get(key) or 0)
            if chash and not args.dry_run:
                set_seed_marker(km, seed.name, chash)

        status = km.status() if hasattr(km, "status") else {}
        result["nodes_after"] = int(status.get("nodes") or 0)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Brain: {result['brain_dir']}")
        print(f"Grafo: {result['db']}")
        print(f"Nodos: {result['nodes_before']} -> {result['nodes_after']}"
              + (" (dry-run)" if args.dry_run else ""))
        for item in result["seeds"]:
            name = Path(item["file"]).name
            if item.get("error"):
                print(f"  {name}: ERROR {item['error']}")
                continue
            if item.get("already"):
                print(f"  {name}: ya importado (misma version)")
                continue
            print(f"  {name}: nuevos={item['new']} actualizados={item['updated']} "
                  f"omitidos={item['skipped']} (nodos bundle={item['total']}, "
                  f"aristas={item.get('edges')})")
        if result.get("skipped"):
            print("Base: " + result["skipped"])
        if result.get("warning"):
            print("AVISO: " + result["warning"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
