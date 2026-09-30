#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BaterÃ­a compleja de pruebas de herramientas â€” Zen (cloud) vs local (Ollama)"""
import json, time, urllib.request, urllib.error, sys, pathlib, os

# consola Windows cp1252: imprime flechas/etc de respuestas LLM sin crashear
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8765"
PROJECT = pathlib.Path(__file__).resolve().parent

def req(method, path, body=None, timeout=90):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type":"application/json"} if body is not None else {}
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            return {"ok": True, "status": resp.status, "body": json.loads(raw) if raw else {}, "raw": raw, "ms": int((time.monotonic()-t0)*1000)}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try: j = json.loads(raw)
        except: j = {"raw": raw}
        return {"ok": False, "status": e.code, "body": j, "raw": raw, "ms": int((time.monotonic()-t0)*1000)}
    except Exception as e:
        return {"ok": False, "status": 0, "body": {"error": str(e)}, "raw": str(e), "ms": int((time.monotonic()-t0)*1000)}

def pp(label, r, clip=1200):
    status = "OK" if r["ok"] else f"FAIL {r['status']}"
    ms = r["ms"]
    print(f"\n[{label}] {status} {ms}ms")
    raw = r["raw"] or json.dumps(r["body"], ensure_ascii=False)
    print(raw[:clip])
    if len(raw) > clip: print(f"... (+{len(raw)-clip} chars)")

# 0. health + providers snapshot
pp("0 health", req("GET","/api/health"))
pp("0 providers", req("GET","/api/providers"))

# 1. FS directo â€” registrar baterÃ­a en workspace para validar roundtrip real
uniq = str(int(time.time()))
tests = []

# 1a write_file directo via /api/fs/write
pp("1a PUT /api/fs/write workspace/bateria_zen.txt", req("PUT","/api/fs/write", {"path":"workspace/bateria_zen.txt","content":f"hola zen {uniq}\nlinea 2\n"}, timeout=10))
# 1b read back
pp("1b GET /api/fs/read bateria_zen.txt", req("GET", f"/api/fs/read?path=workspace/bateria_zen.txt"))
# 1c list
pp("1c GET /api/fs/list workspace", req("GET","/api/fs/list?path=workspace"))
# 1d exec ls
pp("1d POST /api/exec ls", req("POST","/api/exec", {"command":"ls -la", "cwd":"workspace"}, timeout=10))
# 1e exec python
pp("1e POST /api/exec python sum", req("POST","/api/exec", {"command":"python -c \"print(sum(range(101)))\"", "cwd":"workspace"}, timeout=10))
# 1f scope deny â€” .env
pp("1f DENY .env (expect 403)", req("GET","/api/fs/read?path=.env"))
# 1g scope deny â€” traversal
pp("1g DENY traversal ../../ (expect 403)", req("GET","/api/fs/read?path=../../etc/hosts"))
# 1h exec destroy blocked
pp("1h EXEC blocked rm -rf / (expect deny)", req("POST","/api/exec", {"command":"rm -rf / --no-preserve-root", "cwd":"workspace"}))

# 2. Chat via Zen â€” force_tools, debe usar write_file/read_file/exec reales
# Mensajes diseÃ±ados para forzar dispatch (herramientas registradas: write_file, read_file, list_dir, exec, echo)

def chat_test(label, body, expect_tool=None):
    r = req("POST","/api/chat", body, timeout=90)
    pp(label, r, clip=2500)
    if r["ok"]:
        b = r["body"]
        reply = b.get("reply","")
        steps = b.get("steps",[])
        iters = b.get("iterations",0)
        mode = b.get("mode","")
        # verificar si usÃ³ herramienta
        tools_used = [s.get("tool_used") for s in steps if s.get("tool_used")]
        print(f"  -> mode={mode} iterations={iters} tools_used={tools_used}")
        if expect_tool:
            hit = any(expect_tool in (t or "") for t in tools_used) or (expect_tool in reply)
            print(f"  -> expect_tool={expect_tool} {'HIT' if hit else 'MISS'}")
            tests.append((label, hit))
        else:
            tests.append((label, True))
        # verificar archivo creado si corresponde
        if "prueba_zen" in label.lower() or "prueba_local" in label.lower():
            p = PROJECT / "workspace" / ("prueba_zen.txt" if "zen" in label.lower() else "prueba_local.txt")
            exists = p.exists()
            content = p.read_text(encoding="utf-8", errors="replace")[:500] if exists else "<no existe>"
            print(f"  -> archivo {p.name} exists={exists} preview: {content[:200]!r}")
    else:
        tests.append((label, False))
    return r

# 2a Zen â€” crear archivo
chat_test("2a CHAT ZEN write_file (space-bunny-free)",
    {"message":"Usa la herramienta write_file para crear el archivo workspace/prueba_zen.txt con contenido exactamente 'zen-ok 9876'. No inventes, usa write_file. Luego confirma.", "model":"space-bunny-free", "force_tools": True},
    expect_tool="write_file")

# 2b Zen â€” leerlo de vuelta
chat_test("2b CHAT ZEN read_file",
    {"message":"Usa la herramienta read_file para leer workspace/prueba_zen.txt y dime su contenido exacto. Usa read_file.", "model":"space-bunny-free", "force_tools": True},
    expect_tool="read_file")

# 2c Zen â€” list_dir
chat_test("2c CHAT ZEN list_dir",
    {"message":"Usa la herramienta list_dir para listar workspace y dime cuantos archivos hay. Usa list_dir con path=workspace.", "model":"space-bunny-free", "force_tools": True},
    expect_tool="list_dir")

# 2d Zen â€” exec
chat_test("2d CHAT ZEN exec",
    {"message":"Usa la herramienta exec para ejecutar el comando 'python -c \"print(40+2)\"' en cwd workspace y dime el resultado. Usa exec.", "model":"space-bunny-free", "force_tools": True},
    expect_tool="exec")

# 3. Mismos 4 pero via local (nexus-director-v6) para comparar latencia/calidad
chat_test("3a CHAT LOCAL write_file (nexus-director-v6)",
    {"message":"Usa la herramienta write_file para crear el archivo workspace/prueba_local.txt con contenido exactamente 'local-ok 1234'. Usa write_file.", "model":"nexus-director-v6", "force_tools": True},
    expect_tool="write_file")

chat_test("3b CHAT LOCAL read_file",
    {"message":"Usa la herramienta read_file para leer workspace/prueba_local.txt y dime su contenido. Usa read_file.", "model":"nexus-director-v6", "force_tools": True},
    expect_tool="read_file")

# 4. Multi-step: Zen debe encadenar write -> exec -> read
chat_test("4 MULTI-STEP ZEN (write+exec+read)",
    {"message":"Haz esto en orden usando herramientas: 1) write_file workspace/multi_zen.txt con contenido 'multi-42', 2) exec 'cat multi_zen.txt' en workspace, 3) read_file workspace/multi_zen.txt. Usa cada herramienta y reporta resultados.", "model":"space-bunny-free", "force_tools": True},
    expect_tool="write_file")

# resumen
print("\n" + "="*70)
print("RESUMEN")
for label, ok in tests:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
passed = sum(1 for _,ok in tests if ok)
print(f"\n{passed}/{len(tests)} tool-use hits")
print("="*70)
