"""Instala y verifica los MCP del bundle de nexus-v3.

Uso:
    python scripts/setup_mcp.py               # verifica (no instala nada)
    python scripts/setup_mcp.py --fix         # instala lo instalable + handshake
    python scripts/setup_mcp.py --fix --json  # reporte JSON
    python scripts/setup_mcp.py --emit-client # snippet para el cliente MCP

Deja el resultado en data/mcp_bundle/status.json para que la UI y el doctor
lo muestren sin reinstalar.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.core.mcp_setup import (  # noqa: E402
    ensure_uv,
    manifest_path,
    run_mcp_setup,
    status_path,
    write_status,
)

ICON = {"ok": "OK  ", "missing": "FALTA", "needs_key": "CLAVE", "manual": "MANUAL",
        "disabled": "OFF ", "error": "ERROR", "remote": "REMOTO"}


def client_snippet() -> dict:
    """Config lista para pegar en un cliente MCP (rutas absolutas de ESTA maquina)."""
    py = os.path.join(ROOT, ".venv-ft314", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    return {
        "nexus-v3": {
            "command": py,
            "args": ["-m", "src.mcp_server"],
            "cwd": ROOT,
            "environment": {"PYTHONUNBUFFERED": "1"},
        }
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Setup de los MCP de nexus-v3")
    ap.add_argument("--fix", action="store_true", help="instala lo instalable")
    ap.add_argument("--json", action="store_true", help="salida JSON")
    ap.add_argument("--emit-client", action="store_true",
                    help="imprime el snippet de config para un cliente MCP y sale")
    ap.add_argument("--no-status", action="store_true", help="no escribe status.json")
    args = ap.parse_args()

    if args.emit_client:
        print(json.dumps({"mcpServers": client_snippet()}, ensure_ascii=False, indent=2))
        return 0

    if args.fix:
        uv = ensure_uv()
        if not args.json:
            print("uvx propio: " + (uv.get("path") or "NO DISPONIBLE")
                  + ("  (" + uv.get("action", "") + ")" if uv.get("action") else ""))

    report = run_mcp_setup(fix=args.fix, resolve=True, ensure_uv_binary=False)
    report["manifest"] = str(manifest_path())
    report["project_root"] = ROOT
    if not args.no_status:
        write_status(report)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ready") else 2

    print("\nMCP del bundle ({}):".format(manifest_path()))
    for e in report.get("entries", []):
        name = e["name"]
        kind = e.get("kind", "")
        st = e.get("status", "?")
        detail = (e.get("detail") or "").replace("\n", " ")[:90]
        print("  {0:<7} {1:<18} {2:<10} {3}".format(ICON.get(st, st), name, kind, detail))
    s = report.get("summary", {})
    print("\nResumen: " + ", ".join(f"{k}={v}" for k, v in sorted(s.items())))
    if s.get("remote"):
        print("Remotos: configurados pero no usables por nexus-v3 (el cliente interno "
              "solo lanza MCP stdio): " +
              ", ".join(e["name"] for e in report["entries"] if e["status"] == "remote"))
    if report.get("needs_key"):
        print("Requieren clave propia: " + ", ".join(report["needs_key"]))
    if report.get("manual"):
        print("Manuales: " + ", ".join(report["manual"]))
    print("Estado: " + ("LISTO" if report.get("ready") else "INCOMPLETO")
          + f"  (guardado en {status_path()})")
    if not args.fix:
        print("\nPara instalar lo que falta: python scripts/setup_mcp.py --fix")
    return 0 if report.get("ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
