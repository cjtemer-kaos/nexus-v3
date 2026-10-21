"""Genera el manifiesto de update (archivos + sha256) para detectar cambios.

El manifiesto NO se aplica solo: el endpoint /api/updates/check solo compara.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.updater import build_manifest, validate_manifest  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Manifiesto de update de Nexus v3")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--out", default=str(ROOT / "updates-manifest.json"))
    ap.add_argument("--version", default="")
    ap.add_argument("--commit", default="")
    args = ap.parse_args(argv)

    try:
        manifest = build_manifest(args.root, version=args.version, commit=args.commit)
        validate_manifest(manifest)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "ok": True,
        "path": str(out),
        "bytes": out.stat().st_size,
        "version": manifest["version"],
        "commit": manifest["commit"],
        "counts": manifest["counts"],
        "content_hash": manifest["content_hash"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
