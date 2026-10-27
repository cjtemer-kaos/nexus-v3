"""Genera (o verifica) el contrato TS de la API a partir de `src/api/server.py`.

    python scripts/gen_api_contract.py           # escribe ui/src/lib/api-contract.generated.ts
    python scripts/gen_api_contract.py --check   # exit 1 si el archivo quedó viejo

Con `--check` no escribe nada: es el modo que usa el test de frescura y el que
sirve en CI para que el contrato no derive.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.api_contract import CONTRACT_PATH, extract_routes, render, contract_version  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Contrato TS de la API de nexus-v3")
    ap.add_argument("--check", action="store_true", help="no escribe: falla si el archivo está desactualizado")
    ap.add_argument("--out", default=str(CONTRACT_PATH), help="ruta del .ts generado")
    args = ap.parse_args(argv)

    destino = Path(args.out)
    contenido = render()
    rutas = extract_routes((ROOT / "src" / "api" / "server.py").read_text(encoding="utf-8"))
    version = contract_version(rutas)

    if args.check:
        actual = destino.read_text(encoding="utf-8") if destino.is_file() else ""
        if actual != contenido:
            print(f"contrato desactualizado: {destino} (versión esperada {version})")
            print("regenerá con: .venv-ft314\\Scripts\\python.exe scripts\\gen_api_contract.py")
            return 1
        print(f"contrato al día: {len(rutas)} rutas, versión {version}")
        return 0

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(contenido, encoding="utf-8", newline="\n")
    print(f"escrito {destino} · {len(rutas)} rutas · versión {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
