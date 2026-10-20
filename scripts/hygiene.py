"""Higiene del proyecto: backups consistentes del brain, workspace y vendor.

Uso:
    python scripts/hygiene.py            # solo informa (dry-run)
    python scripts/hygiene.py --apply    # aplica backup + retención + limpieza

Por qué existe:
- Copiar `knowledge_graph.db` con `shutil.copy` mientras WAL está activo puede
  producir un backup corrupto. Acá se usa la API de backup de sqlite3 (consistente).
- `brain/*.bak-*` y `data/backups/brain/*` crecían sin límite (~165 MB de copias).
  Se aplica una política de retención (default: 3 por base).
- `workspace/` acumulaba `__pycache__`, directorios vacíos y carpetas accidentales
  (`-p`, `pkg`, `workspace/workspace`) de comandos mal parseados.
- `vendor/llama.cpp` (556 MB) ya no se usa (migración a Ollama): el reporte avisa
  si está presente y qué binarios faltan, sin borrar nada por su cuenta.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN = ROOT / "brain"
BACKUPS = ROOT / "data" / "backups" / "brain"
LEGACY = BACKUPS / "legacy"
WORKSPACE = ROOT / "workspace"
VENDOR = ROOT / "vendor" / "llama.cpp"

# Basura conocida del sandbox (nunca contiene trabajo real del usuario).
WORKSPACE_JUNK_NAMES = {"-p", "pkg", "__pycache__"}

# Índices 100% regenerables (se reconstruyen con rag_index): respaldarlos infla
# el disco sin proteger conocimiento. El resto de las bases sí es irrecuperable.
BACKUP_EXCLUDE = {"rag_index.db"}


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _backup_db(src: Path, dst_dir: Path) -> tuple[bool, str]:
    """Backup consistente de una base SQLite (incluye WAL). Devuelve (ok, mensaje)."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{src.stem}-{_stamp()}.db"
    try:
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as src_conn:
            with sqlite3.connect(dst) as dst_conn:
                src_conn.backup(dst_conn)
        with sqlite3.connect(dst) as check:
            result = check.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            dst.unlink(missing_ok=True)
            return False, f"{src.name}: integrity_check={result[0] if result else 'sin resultado'}"
        return True, f"{src.name} -> {dst.relative_to(ROOT)} ({dst.stat().st_size // 1024} KB)"
    except Exception as e:  # base bloqueada, corrupta o sin permisos
        dst.unlink(missing_ok=True)
        return False, f"{src.name}: {type(e).__name__}: {e}"


def _prune(files: list[Path], keep: int, apply: bool) -> list[str]:
    """Borra los backups más viejos dejando `keep` por familia (nombre antes del stamp)."""
    groups: dict[str, list[Path]] = {}
    for f in files:
        if not f.exists():
            continue
        # `knowledge_graph.db.bak-viejo`, `knowledge_graph-20261019_213639.db` y
        # `knowledge_graph.db` comparten la misma familia.
        name = f.name.split(".bak-")[0]
        name = re.sub(r"-\d{8}_\d{6}.*$", "", name)
        key = name[:-3] if name.endswith(".db") else name
        groups.setdefault(key, []).append(f)
    removed: list[str] = []
    for group in groups.values():
        group.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in group[keep:]:
            removed.append(f"{old.relative_to(ROOT)} ({old.stat().st_size // 1024 // 1024} MB)")
            if apply:
                old.unlink(missing_ok=True)
    return removed


def _clean_workspace(apply: bool) -> list[str]:
    actions: list[str] = []
    if not WORKSPACE.exists():
        return actions
    for path in sorted(WORKSPACE.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and path.name in WORKSPACE_JUNK_NAMES:
            actions.append(f"borrar {path.relative_to(ROOT)}/")
            if apply:
                shutil.rmtree(path, ignore_errors=True)
        elif path.is_dir() and not any(path.iterdir()):
            actions.append(f"borrar directorio vacio {path.relative_to(ROOT)}/")
            if apply:
                path.rmdir()
    return actions


def _vendor_report() -> dict:
    if not VENDOR.exists():
        return {"present": False, "size_mb": 0, "missing": []}
    required = ["llama-server.exe", "llama.dll", "llama-common.dll", "ggml.dll", "ggml-base.dll"]
    size = sum(f.stat().st_size for f in VENDOR.rglob("*") if f.is_file())
    return {
        "present": True,
        "size_mb": round(size / 1024 / 1024, 1),
        "missing": [r for r in required if not (VENDOR / r).exists()],
        "in_use": False,
        "note": "llama.cpp no se usa desde la migracion a Ollama; se puede restaurar desde backups.",
    }


def run(apply: bool = False, keep: int = 3, as_json: bool = False) -> dict:
    report: dict = {"applied": apply, "keep": keep, "backups": [], "pruned": [], "workspace": [], "vendor": _vendor_report()}

    if BRAIN.exists():
        for db in sorted(BRAIN.glob("*.db")):
            if db.name.endswith(("-wal", "-shm")) or db.name in BACKUP_EXCLUDE:
                continue
            if apply:
                ok, msg = _backup_db(db, BACKUPS)
                report["backups"].append({"ok": ok, "detail": msg})
            else:
                report["backups"].append({"ok": None, "detail": f"{db.name}: se respaldaria con sqlite3.backup()"})

    brain_legacy = sorted(BRAIN.glob("*.bak-*"), key=lambda p: p.stat().st_mtime, reverse=True) if BRAIN.exists() else []
    if apply and brain_legacy:
        LEGACY.mkdir(parents=True, exist_ok=True)
        for old in brain_legacy:
            target = LEGACY / old.name
            if not target.exists():
                shutil.move(str(old), str(target))
    _seen: set = set()
    legacy = []
    for p in list(LEGACY.glob("*.bak-*") if LEGACY.exists() else []) + brain_legacy:
        if p.exists() and p not in _seen:
            _seen.add(p)
            legacy.append(p)
    legacy.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    backups = sorted(BACKUPS.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    report["pruned"] = _prune(legacy + backups, keep=keep, apply=apply)
    report["workspace"] = _clean_workspace(apply)

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"== higiene {'(APLICADO)' if apply else '(dry-run: usar --apply)'} ==")
        for b in report["backups"]:
            tag = "backup OK  " if b["ok"] else ("backup ERR " if b["ok"] is False else "backup --- ")
            print("  " + tag + b["detail"])
        v = report["vendor"]
        print(f"  vendor/llama.cpp: {'presente ' + str(v['size_mb']) + ' MB (sin uso)' if v['present'] else 'ausente'}"
              + (f" faltan {v['missing']}" if v.get("missing") else ""))
        print(f"  retencion: {len(report['pruned'])} backup(s) a borrar (keep={keep})")
        for p in report["pruned"]:
            print("    - " + p)
        for w in report["workspace"]:
            print("  workspace: " + w)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Higiene de brain/workspace/vendor")
    ap.add_argument("--apply", action="store_true", help="aplica los cambios (sin esto es dry-run)")
    ap.add_argument("--keep", type=int, default=3, help="backups a conservar por base (default 3)")
    ap.add_argument("--json", action="store_true", help="salida JSON")
    args = ap.parse_args(argv)
    report = run(apply=args.apply, keep=max(1, args.keep), as_json=args.json)
    failed = [b for b in report["backups"] if b["ok"] is False]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
