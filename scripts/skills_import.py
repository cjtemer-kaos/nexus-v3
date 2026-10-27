"""Importa skills (arbol de carpetas con SKILL.md) al catalogo local de nexus-v3.

El catalogo vive dentro del proyecto (`data/skills`, gitignoreado porque pesa
cientos de MB) y el harness nunca lee skills de otra instalacion. Este script es
la unica via para poblarlo: copia desde el origen que le pases y deja el arbol
sin basura (`.git`, `node_modules`, `__pycache__`).

Uso:
    python scripts/skills_import.py --from D:\\ruta\\al\\hub
    python scripts/skills_import.py --from A --from B        # varias fuentes
    python scripts/skills_import.py --from-opencode          # ~/.config/opencode/skills + ~/.claude/skills
    python scripts/skills_import.py --from D:\\ruta\\al\\hub --dry-run
    python scripts/skills_import.py --prune          # solo limpia el destino
    python scripts/skills_import.py --from RUTA --clean   # borra el destino antes

Variables de entorno:
    NEXUS_V3_SKILLS_SRC   origen por defecto si no pasas --from (uno o varios con ;)

Despues de importar, el server lo ve sin reiniciar (`/api/skills?refresh=true`)
o reiniciando; el indice tiene TTL de 5 minutos.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DESTINO = RAIZ / "data" / "skills"
BASURA = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache",
          ".mypy_cache", ".ruff_cache"}


def _limpiar_basura(base: Path) -> int:
    borrados = 0
    for dirpath, dirnames, _ in os.walk(base, topdown=True):
        for d in list(dirnames):
            if d in BASURA:
                objetivo = Path(dirpath) / d
                shutil.rmtree(objetivo, ignore_errors=True)
                dirnames.remove(d)
                borrados += 1
    return borrados


def _contar_skills(base: Path) -> int:
    return sum(1 for _ in base.rglob("SKILL.md"))


def _cockpit_dirs() -> list[Path]:
    """Carpetas donde opencode/Claude Code instalan skills en esta maquina.

    Orden importa: el ultimo origen gana ante la misma ruta relativa, y
    `~/.config/opencode/skills` es donde el usuario tiene sus skills propias
    (obs, x64dbg-reversing, sprite-gen...), asi que va al final.
    """
    home = Path.home()
    return [
        home / ".claude" / "skills",
        home / ".config" / "opencode" / "skills",
    ]


def _fuentes(args, ap) -> list[Path]:
    crudos: list[str] = list(args.origen or [])
    if getattr(args, "from_opencode", False):
        for d in _cockpit_dirs():
            if d.is_dir():
                crudos.append(str(d))
            else:
                print(f"aviso: no existe {d}")
    env = os.environ.get("NEXUS_V3_SKILLS_SRC", "")
    if not crudos and env:
        crudos = [p for p in env.split(os.pathsep) if p.strip()]
    return [Path(p).expanduser().resolve() for p in crudos if str(p).strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Importa skills al catalogo de nexus-v3.")
    ap.add_argument("--from", dest="origen", action="append", default=[],
                    help="carpeta de origen (repetible; busca SKILL.md en todo el arbol)")
    ap.add_argument("--from-opencode", action="store_true",
                    help="agrega ~/.config/opencode/skills y ~/.claude/skills si existen")
    ap.add_argument("--to", dest="destino", default=str(DESTINO),
                    help=f"carpeta destino (default: {DESTINO})")
    ap.add_argument("--dry-run", action="store_true", help="solo informa, no copia")
    ap.add_argument("--clean", action="store_true", help="borra el destino antes de copiar")
    ap.add_argument("--prune", action="store_true", help="solo limpia basura del destino")
    args = ap.parse_args(argv)

    destino = Path(args.destino).resolve()
    try:
        destino.relative_to(RAIZ)
    except ValueError:
        print(f"ERROR: el destino debe estar dentro de {RAIZ} (recibido {destino})")
        return 2

    if args.prune:
        if not destino.is_dir():
            print(f"No existe {destino}")
            return 1
        n = _limpiar_basura(destino)
        print(f"Basura eliminada: {n} carpetas. Skills: {_contar_skills(destino)}")
        return 0

    origenes = _fuentes(args, ap)
    if not origenes:
        print("ERROR: falta --from (o --from-opencode, o NEXUS_V3_SKILLS_SRC).")
        return 2

    validos: list[Path] = []
    for origen in origenes:
        if not origen.is_dir():
            print(f"ERROR: no existe el origen {origen}")
            return 2
        if origen == destino or destino in origen.parents or origen in destino.parents:
            print(f"ERROR: origen y destino no pueden estar anidados ({origen}).")
            return 2
        if not _contar_skills(origen):
            print(f"ERROR: no hay SKILL.md en {origen}")
            return 1
        validos.append(origen)

    for origen in validos:
        mb = sum(p.stat().st_size for p in origen.rglob("*") if p.is_file()) / 1024 / 1024
        print(f"Origen : {origen}  ({_contar_skills(origen)} skills, ~{mb:.1f} MB)")
    print(f"Destino: {destino}")
    if args.dry_run:
        print("dry-run: no se copio nada.")
        return 0

    if args.clean and destino.is_dir():
        print("Borrando destino (--clean)...")
        shutil.rmtree(destino, ignore_errors=True)
    destino.mkdir(parents=True, exist_ok=True)

    copiados = 0
    # El ultimo origen gana ante una misma ruta relativa: pasar las skills
    # personales despues de las de un hub para que queden las personalizadas.
    for origen in validos:
        for dirpath, dirnames, filenames in os.walk(origen, topdown=True):
            dirnames[:] = [d for d in dirnames if d not in BASURA]
            rel = Path(dirpath).relative_to(origen)
            target = destino / rel
            for f in filenames:
                src = Path(dirpath) / f
                dst = target / f
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copiados += 1
                except OSError as e:
                    print(f"  aviso: no se pudo copiar {src.name}: {e}")

    print(f"Copiados: {copiados} archivos.")
    print(f"Skills en destino: {_contar_skills(destino)}")
    print("Listo. El catalogo lo toma en el proximo escaneo (TTL 5 min) o con "
          "/api/skills?refresh=true.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
