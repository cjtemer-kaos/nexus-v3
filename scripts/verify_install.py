"""Verifica que la instalacion de nexus-v3 este completa y funcione.

No depende de ningun cliente externo: arranca el servidor MCP propio de
nexus-v3 por stdio, le hace initialize + tools/list + recall, y comprueba los
artefactos, el grafo y el bundle MCP.

Uso:
    python scripts/verify_install.py
    python scripts/verify_install.py --json
    python scripts/verify_install.py --skip-mcp     (mas rapido)

Exit 0 = todo lo obligatorio paso. Exit 1 = hay fallas.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESULTS: list[dict] = []
args_json = False


def check(name: str, ok: bool, detail: str = "", *, required: bool = True) -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail,
                    "required": required})
    if not args_json:
        tag = "PASS" if ok else ("FALLA" if required else "AVISO")
        print(f"  [{tag:<5}] {name}" + (f" :: {detail}" if detail else ""))
    return bool(ok)


def py_exe() -> str:
    for rel in (Path(".venv-ft314") / "Scripts" / "python.exe",
                Path(".venv-ft314") / "bin" / "python",
                Path(".venv") / "Scripts" / "python.exe",
                Path(".venv") / "bin" / "python"):
        p = ROOT / rel
        if p.exists():
            return str(p)
    return sys.executable


def check_python() -> None:
    exe = py_exe()
    check("Python del proyecto", Path(exe).exists(), exe)
    r = subprocess.run([exe, "-c",
                        "import sys,fastapi,uvicorn,mcp,yaml,httpx;"
                        "print(sys.version.split()[0])"],
                       capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0
    check("Dependencias Python", ok,
          (r.stdout or r.stderr).strip().splitlines()[-1][:120] if (r.stdout or r.stderr) else "")


def check_artifacts() -> None:
    for rel, label, hint in (
        (Path("electron") / "dist" / "main.js", "Electron compilado", "npm run build:electron"),
        (Path("ui") / "dist" / "index.html", "UI compilada", "npm run build:ui"),
        (Path("node_modules") / "electron", "Electron instalado", "npm install"),
        (Path("ui") / "node_modules", "Dependencias de UI", "npm install --prefix ui"),
    ):
        p = ROOT / rel
        check(label, p.exists(), str(rel) if p.exists() else f"falta {rel} -> {hint}")


def check_config_and_graph() -> None:
    try:
        from src.api.server import _load_config_safe
        cfg = _load_config_safe()
        check("Configuracion valida", isinstance(cfg, dict) and bool(cfg),
              f"{len(cfg)} claves")
    except Exception as e:
        check("Configuracion valida", False, f"{type(e).__name__}: {e}")
    try:
        from src.core.knowledge_memory import KnowledgeMemory
        km = KnowledgeMemory()
        st = km.status() if hasattr(km, "status") else {}
        nodes = int(st.get("nodes") or 0)
        seed_dir = ROOT / "brain" / "seed"
        has_seed = seed_dir.is_dir() and any(
            p.name.endswith((".nexuskg.json", ".nexuskg.json.gz"))
            for p in seed_dir.iterdir() if p.is_file())
        check("Grafo con la base compartida", nodes >= (100 if has_seed else 1),
              f"{nodes} nodos en {km.db_path.name}")
        hits = km.fts_search("instalacion", limit=3) if hasattr(km, "fts_search") else []
        check("Busqueda en el grafo (FTS)", bool(hits), f"{len(hits or [])} resultados")
    except Exception as e:
        check("Grafo con la base compartida", False, f"{type(e).__name__}: {e}")
        check("Busqueda en el grafo (FTS)", False, "no se pudo abrir el grafo")


def check_mcp_bundle() -> None:
    try:
        from src.core.mcp_setup import run_mcp_setup
        rep = run_mcp_setup(fix=False, resolve=True)
        s = rep.get("summary", {})
        ok = rep.get("ready", False)
        check("Bundle MCP verificado", ok,
              ", ".join(f"{k}={v}" for k, v in sorted(s.items())))
        if rep.get("needs_key"):
            check("MCP que requieren clave propia", True,
                  ", ".join(rep["needs_key"]), required=False)
    except Exception as e:
        check("Bundle MCP verificado", False, f"{type(e).__name__}: {e}")


def _mcp_handshake(timeout: float) -> dict:
    exe = py_exe()
    out, err = [], []

    def reader(stream, sink):
        for raw in stream:
            sink.append(raw.rstrip("\n"))

    proc = subprocess.Popen([exe, "-m", "src.mcp_server"], cwd=str(ROOT),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)
    threading.Thread(target=reader, args=(proc.stdout, out), daemon=True).start()
    threading.Thread(target=reader, args=(proc.stderr, err), daemon=True).start()

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def wait(mid, secs):
        end = time.time() + secs
        while time.time() < end:
            for line in list(out):
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("id") == mid:
                    return msg
            time.sleep(0.15)
        return None

    info = {"ok": False, "tools": 0, "recall": "", "stderr": "", "error": ""}
    try:
        t0 = time.time()
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "verify-install", "version": "1"}}})
        init = wait(1, timeout)
        if not init:
            info["error"] = f"initialize sin respuesta en {timeout:.0f}s"
            return info
        info["init_s"] = round(time.time() - t0, 1)
        info["server"] = (init.get("result") or {}).get("serverInfo") or {}
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = wait(2, 60)
        names = [t.get("name") for t in ((tools or {}).get("result") or {}).get("tools", [])]
        info["tools"] = len(names)
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "recall", "arguments": {"query": "instalacion", "limit": 2}}})
        call = wait(3, 90)
        res = (call or {}).get("result") or {}
        text = "".join(c.get("text", "") for c in res.get("content", []) or [])
        info["recall"] = text[:160]
        info["ok"] = bool(names) and bool(text.strip()) and not res.get("isError")
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        time.sleep(0.5)
        if proc.poll() is None:
            proc.kill()
        info["stderr"] = "\n".join(err[-6:])
    return info


def check_mcp_server(timeout: float) -> None:
    info = _mcp_handshake(timeout)
    if info.get("error"):
        check("Servidor MCP propio (stdio)", False, info["error"])
        return
    srv = info.get("server") or {}
    check("Servidor MCP propio (stdio)", info["ok"],
          f"{srv.get('name', '?')} {srv.get('version', '')} · "
          f"{info['tools']} tools · init {info.get('init_s', '?')}s")
    check("Tool recall responde", bool(info.get("recall")), info.get("recall", "")[:100])


def check_backend_running() -> None:
    """Opcional: si la app esta abierta, el endpoint de salud debe responder."""
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8765/api/health", timeout=2.0)
        ok = r.status_code == 200
        check("Backend en :8765 (si esta abierto)", ok,
              r.text[:80], required=False)
    except Exception:
        check("Backend en :8765 (si esta abierto)", True,
              "no esta corriendo (normal si no abriste la app)", required=False)


def main() -> int:
    global args_json
    ap = argparse.ArgumentParser(description="Verificacion de instalacion de nexus-v3")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-mcp", action="store_true", help="no arranca el servidor MCP")
    ap.add_argument("--timeout", type=float, default=120.0)
    ns = ap.parse_args()
    args_json = ns.json

    if not args_json:
        print(f"Verificando nexus-v3 en {ROOT}")
    check_python()
    check_artifacts()
    check_config_and_graph()
    check_mcp_bundle()
    if not ns.skip_mcp:
        check_mcp_server(ns.timeout)
    check_backend_running()

    fails = [r for r in RESULTS if r["required"] and not r["ok"]]
    if args_json:
        print(json.dumps({"ok": not fails, "failures": fails, "checks": RESULTS},
                         ensure_ascii=False, indent=2))
    else:
        print(f"\nResultado: {len(RESULTS) - len(fails)}/{len(RESULTS)} OK"
              + ("" if not fails else f" · {len(fails)} falla(s)"))
        if fails:
            print("Arreglo sugerido:")
            for f in fails:
                print(f"  - {f['name']}: {f['detail']}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
