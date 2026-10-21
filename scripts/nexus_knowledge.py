"""Intercambio portable del grafo de conocimiento (.nexuskg.json).

Uso:
  nexus_knowledge.py export --out cerebro.nexuskg.json
  nexus_knowledge.py export --categories architecture,note
  nexus_knowledge.py info cerebro.nexuskg.json
  nexus_knowledge.py import cerebro.nexuskg.json            # dry-run
  nexus_knowledge.py import cerebro.nexuskg.json --apply    # escribe
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.knowledge_bundle import (  # noqa: E402
    export_bundle, import_bundle, read_bundle, write_bundle,
)
from src.core.knowledge_memory import KnowledgeMemory, get_knowledge_memory  # noqa: E402


def _km(args):
    if getattr(args, "db", None):
        return KnowledgeMemory(Path(args.db), rag_index=False)
    return get_knowledge_memory()


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_export(args) -> int:
    cats = [c.strip() for c in (args.categories or "").split(",") if c.strip()]
    bundle = export_bundle(_km(args), categories=cats,
                           include_sessions=bool(args.include_sessions))
    if args.out in ("-", ""):
        _print(bundle)
        return 0
    p = write_bundle(bundle, args.out)
    print(f"exportado: {p} ({bundle['counts']['nodes']} nodos, "
          f"{bundle['counts']['edges']} aristas, {p.stat().st_size} bytes)",
          file=sys.stderr)
    _print({"ok": True, "path": str(p), "counts": bundle["counts"],
            "content_hash": bundle["content_hash"]})
    return 0


def cmd_info(args) -> int:
    b = read_bundle(args.path)
    _print({
        "schema": b.get("schema"),
        "created_at": b.get("created_at"),
        "source": b.get("source"),
        "counts": b.get("counts"),
        "content_hash": b.get("content_hash"),
    })
    return 0


def cmd_import(args) -> int:
    b = read_bundle(args.path)
    res = import_bundle(_km(args), b, dry_run=not args.apply,
                        overwrite=bool(args.overwrite),
                        source_label=Path(args.path).name)
    _print(res)
    if not args.apply:
        print("dry-run: no se escribio nada. Repeti con --apply para importar.",
              file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Bundles portables del grafo Nexus v3")
    ap.add_argument("--db", help="knowledge_graph.db alternativo (default: brain activo)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="exportar el grafo a un bundle")
    e.add_argument("--out", default="-", help="archivo destino ('-' = stdout)")
    e.add_argument("--categories", default="", help="categorias separadas por coma")
    e.add_argument("--include-sessions", action="store_true", help="incluir nodos de sesion")
    e.set_defaults(func=cmd_export)

    i = sub.add_parser("import", help="importar/mergear un bundle")
    i.add_argument("path")
    i.add_argument("--apply", action="store_true", help="escribir (default: dry-run)")
    i.add_argument("--overwrite", action="store_true", help="pisar aunque el bundle no sea mas nuevo")
    i.set_defaults(func=cmd_import)

    n = sub.add_parser("info", help="validar y describir un bundle")
    n.add_argument("path")
    n.set_defaults(func=cmd_info)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
