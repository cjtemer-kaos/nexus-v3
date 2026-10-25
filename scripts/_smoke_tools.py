"""Prueba de humo de TODAS las tools del registry.

No mide calidad de respuesta: mide que la tool exista, acepte sus argumentos y
devuelva algo coherente (o falle con un error legible) en vez de colgarse, tirar
una excepción rara o apuntar a un endpoint que no existe.

Uso:
    python scripts/_smoke_tools.py            # todas menos las caras (LLM)
    python scripts/_smoke_tools.py --slow     # incluye LLM/imagen (tarda)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SANDBOX = ROOT / "data" / "_smoke"


def _leer(resultado) -> str:
    if isinstance(resultado, str):
        return resultado
    try:
        return json.dumps(resultado, ensure_ascii=False)
    except Exception:
        return str(resultado)


def casos(wd: Path, slow: bool) -> list[dict]:
    f = wd / "muestra.txt"
    py = wd / "suma.py"
    base = [
        {"name": "echo", "args": {"text": "hola"}, "timeout": 10},
        {"name": "hello", "args": {"name": "smoke"}, "timeout": 10},
        {"name": "list_dir", "args": {"path": str(wd)}, "timeout": 15},
        {"name": "glob", "args": {"pattern": "*.txt", "path": str(wd)}, "timeout": 15},
        {"name": "read_file", "args": {"path": str(f), "limit": 5}, "timeout": 15},
        {"name": "write_file", "args": {"path": str(wd / "escrito.txt"), "content": "uno\ndos\n"}, "timeout": 20},
        {"name": "edit", "args": {"path": str(wd / "escrito.txt"), "old": "uno", "new": "UNO"}, "timeout": 20},
        {"name": "apply_patch", "args": {"patch": f"*** Begin Patch\n*** Add File: {wd / 'parche.txt'}\n+hola\n*** End Patch\n", "dry_run": True}, "timeout": 20},
        {"name": "code_search", "args": {"query": "def dispatch", "path": str(ROOT / "src" / "core" / "tool_registry.py"), "limit": 3}, "timeout": 30},
        {"name": "repo_map", "args": {"root": str(ROOT / "src" / "security"), "budget_tokens": 800, "max_files": 5}, "timeout": 60},
        {"name": "exec", "args": {"command": "echo humo", "cwd": str(wd), "timeout": 20}, "timeout": 40},
        {"name": "todo_write", "args": {"todos": [{"content": "probar tools", "status": "pending"}]}, "timeout": 10},
        {"name": "learn", "args": {"content": "smoke test de tools 2026-10-25", "topic_key": "smoke:tools", "category": "test"}, "timeout": 30},
        {"name": "recall", "args": {"query": "smoke test de tools", "limit": 2}, "timeout": 30},
        {"name": "agent_list", "args": {}, "timeout": 15},
        {"name": "gema_list", "args": {}, "timeout": 15},
        {"name": "sub_agent_list", "args": {}, "timeout": 15},
        {"name": "team_list", "args": {}, "timeout": 15},
        {"name": "mcp_list", "args": {}, "timeout": 30},
        {"name": "job_start", "args": {"command": "echo trabajo", "cwd": str(wd), "label": "smoke"}, "timeout": 30},
        {"name": "web_fetch", "args": {"url": "https://example.com", "timeout": 20}, "timeout": 45},
        {"name": "web_search", "args": {"query": "ollama api generate images", "limit": 2}, "timeout": 60},
    ]
    # checkpoint_create devuelve un id: las demás se arman después de correrlo.
    base.append({"name": "checkpoint_create", "args": {"message": "smoke"}, "timeout": 30})
    base.append({"name": "checkpoint_list", "args": {"limit": 3}, "timeout": 20})
    # mcp_call contra un server que no existe: debe fallar legible, no colgarse.
    base.append({"name": "mcp_call", "args": {"server": "no-existe", "tool": "x"}, "timeout": 30,
                 "expect_error": ["desconocido"]})
    if slow:
        base += [
            # Delegación: un id mal escrito tiene que fallar cerrado, no caer al default.
            {"name": "agent_run", "args": {"agent_id": "no-existe", "task": "hola"}, "timeout": 60,
             "expect_error": ["agente desconocido"]},
            {"name": "gema_run", "args": {"gema": "no-existe", "task": "hola"}, "timeout": 60,
             "expect_error": ["gema desconocida"]},
            {"name": "sub_agent", "args": {"task": "responde solo: ok", "tools": []}, "timeout": 180},
            {"name": "team_spawn", "args": {"task": "responde solo: ok", "name": "smoke-team"}, "timeout": 180},
            {"name": "roundtable", "args": {"task": "¿1+1?", "seats": 2, "timeout_s": 25}, "timeout": 120},
            # Con ComfyUI apagado la tool debe LANZAR (antes devolvía el error como texto).
            {"name": "image_generate", "args": {"prompt": "un cubo rojo sobre fondo negro"}, "timeout": 300,
             "expect_error": ["image_generate falló", "ComfyUI"]},
        ]
    return base


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slow", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "data" / "_smoke_report.json"))
    a = ap.parse_args()

    from src.api.server import _build_registry, _build_router_from_config, _build_scope, _load_config_safe

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True, exist_ok=True)
    (SANDBOX / "muestra.txt").write_text("linea uno\nlinea dos\nlinea tres\n", encoding="utf-8")
    (SANDBOX / "suma.py").write_text("def suma(a, b):\n    return a + b\n", encoding="utf-8")

    cfg = _load_config_safe()
    scope = _build_scope(cfg)
    router = _build_router_from_config(cfg)
    reg = _build_registry(scope, "smoke-tools", cfg, router=router)

    registro: list[dict] = []
    checkpoint_id = ""
    job_id = ""

    for caso in casos(SANDBOX, a.slow):
        args = dict(caso["args"])
        if caso["name"] == "checkpoint_diff":
            args["checkpoint_id"] = checkpoint_id
        t0 = time.time()
        estado, detalle = "ok", ""
        try:
            res = await asyncio.wait_for(
                reg.dispatch(caso["name"], args, scope=scope), timeout=caso["timeout"])
            texto = _leer(res)
            detalle = texto[:300].replace("\n", " ")
            if caso["name"] == "checkpoint_create":
                try:
                    checkpoint_id = json.loads(texto).get("id", "") or ""
                except Exception:
                    pass
            if caso["name"] == "job_start":
                try:
                    job_id = json.loads(texto).get("id", "") or ""
                except Exception:
                    pass
        except asyncio.TimeoutError:
            estado, detalle = "TIMEOUT", f">{caso['timeout']}s"
        except Exception as e:
            texto_err = f"{type(e).__name__}: {str(e)}"
            esperados = caso.get("expect_error") or []
            if esperados and any(s.lower() in texto_err.lower() for s in esperados):
                estado = "ok"
                detalle = f"fail-closed: {texto_err[:200]}".replace("\n", " ")
                registro.append({"tool": caso["name"], "estado": estado, "detalle": detalle,
                                 "ms": int((time.time() - t0) * 1000)})
                print(f"[{estado:7}] {caso['name']:20} {int((time.time()-t0)*1000):>6}ms  {detalle[:110]}")
                continue
            estado = "ERROR"
            detalle = f"{type(e).__name__}: {str(e)[:280]}".replace("\n", " ")
            registro.append({"tool": caso["name"], "estado": estado, "detalle": detalle,
                             "trace": traceback.format_exc()[-800:], "ms": int((time.time() - t0) * 1000)})
            print(f"[{estado:7}] {caso['name']:20} {detalle[:150]}")
            continue
        if caso.get("expect_error"):
            estado = "ERROR"
            detalle = f"debía fallar ({caso['expect_error']}) y devolvió {detalle[:180]}"
            registro.append({"tool": caso["name"], "estado": estado, "detalle": detalle,
                             "ms": int((time.time() - t0) * 1000)})
            print(f"[{estado:7}] {caso['name']:20} {detalle[:150]}")
            continue
        registro.append({"tool": caso["name"], "estado": estado, "detalle": detalle,
                         "ms": int((time.time() - t0) * 1000)})
        print(f"[{estado:7}] {caso['name']:20} {int((time.time()-t0)*1000):>6}ms  {detalle[:110]}")

    # Segunda pasada: tools que dependen de un id de la primera.
    for nombre, args, exp in (
        ("checkpoint_diff", {"checkpoint_id": checkpoint_id}, 30),
        ("job_read", {"job_id": job_id, "cursor": 0}, 20),
        ("job_wait", {"job_id": job_id, "timeout": 10}, 30),
        ("job_kill", {"job_id": job_id}, 20),
        ("job_list", {}, 15),
    ):
        if not (checkpoint_id if nombre.startswith("checkpoint") else job_id):
            registro.append({"tool": nombre, "estado": "SKIP", "detalle": "sin id previo", "ms": 0})
            print(f"[SKIP   ] {nombre:20} (sin id previo)")
            continue
        t0 = time.time()
        try:
            res = await asyncio.wait_for(reg.dispatch(nombre, args, scope=scope), timeout=exp)
            txt = _leer(res)[:300].replace("\n", " ")
            registro.append({"tool": nombre, "estado": "ok", "detalle": txt, "ms": int((time.time() - t0) * 1000)})
            print(f"[ok     ] {nombre:20} {txt[:110]}")
        except Exception as e:
            detalle = f"{type(e).__name__}: {str(e)[:280]}".replace("\n", " ")
            registro.append({"tool": nombre, "estado": "ERROR", "detalle": detalle, "ms": int((time.time() - t0) * 1000),
                             "trace": traceback.format_exc()[-800:]})
            print(f"[ERROR  ] {nombre:20} {detalle[:150]}")

    Path(a.out).write_text(json.dumps(registro, ensure_ascii=False, indent=1), encoding="utf-8")
    malos = [r for r in registro if r["estado"] in ("ERROR", "TIMEOUT")]
    print(f"\ntotal={len(registro)} ok={sum(1 for r in registro if r['estado']=='ok')} "
          f"error={sum(1 for r in registro if r['estado']=='ERROR')} "
          f"timeout={sum(1 for r in registro if r['estado']=='TIMEOUT')} skip={sum(1 for r in registro if r['estado']=='SKIP')}")
    for r in malos:
        print(f"  - {r['tool']}: {r['detalle'][:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
