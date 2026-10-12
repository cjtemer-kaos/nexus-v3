"""Diagnostico E2E LIVE del harness nexus-v3 (contra el backend en :8765).

Ejercita los flujos reales del usuario y reporta PASS/FAIL. Pensado para correr
a demanda y detectar regresiones ANTES que el usuario.

Uso:  .venv-ft314\\Scripts\\python.exe scripts\\e2e_diagnostic.py [--llm]
      --llm  incluye chequeos que llaman al LLM (lentos)
"""
import sys, os, json, time, io, base64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import httpx

BASE = os.environ.get("NEXUS_E2E_BASE", "http://127.0.0.1:8765")
RUN_LLM = "--llm" in sys.argv

RESULTS = []

def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    return bool(cond)

def section(t):
    print(f"\n=== {t} ===")


def main():
    c = httpx.Client(timeout=60)

    section("1) Health / config")
    t0 = time.time()
    try:
        h = c.get(f"{BASE}/api/health")
        dt = time.time() - t0
        check("health 200", h.status_code == 200, f"{h.status_code}")
        check("health rapido (<1.5s)", dt < 1.5, f"{dt:.2f}s")
        check("health no sondea providers (providers cache)", "providers" in h.json())
    except Exception as e:
        check("health", False, str(e))

    section("2) Providers / modelos")
    try:
        p = c.get(f"{BASE}/api/providers", timeout=60).json()
        provs = list((p.get("providers") or {}).keys())
        check("sin cloud-zen", "cloud-zen" not in provs, str(provs))
        check("opencode-go presente", "opencode-go" in provs)
        check("ollama presente", "ollama" in provs)
        check("hay modelos", len(p.get("models") or []) > 0, f"{len(p.get('models') or [])}")
        bad = [m for m in (p.get("models") or []) if "embed" in (m.get("id") or "").lower()]
        check("sin embeddings en selector", not bad, str([m.get("id") for m in bad][:5]))
    except Exception as e:
        check("providers", False, str(e))

    section("3) Routing (sin LLM)")
    try:
        from src.api.server import _build_router_from_config, _load_config_safe
        from src.core.provider_router import is_chat_model_id
        r = _build_router_from_config(_load_config_safe())
        for hint in ["infer", "none", "coding", "vision"]:
            rt = r.resolve(task_hint=hint)
            mid = getattr(rt, "model", None)
            check(f"resolve({hint}) no-embed", mid and is_chat_model_id(mid), str(mid))
        rv = r.resolve(task_hint="vision")
        check("resolve(vision) existe", rv is not None, getattr(rv, "model", None))
    except Exception as e:
        check("routing", False, str(e))

    section("4) Tools registradas + schemas")
    try:
        from src.api.server import _build_registry, _build_scope, _load_config_safe, _build_router_from_config
        cfg = _load_config_safe()
        reg = _build_registry(_build_scope(cfg), "diag", cfg, router=_build_router_from_config(cfg))
        tools = reg.to_openai_tools()
        names = [t["function"]["name"] for t in tools]
        check("tools >= 15", len(tools) >= 15, str(len(tools)))
        check("delegacion presente", all(x in names for x in ["gema_list", "agent_list", "gema_run", "agent_run"]))
        check("tools base presentes", all(x in names for x in ["read_file", "write_file", "list_dir", "exec", "learn", "recall"]))
        check("edit (str_replace) presente", "edit" in names)
        check("web_search presente", "web_search" in names)
        check("code_search presente", "code_search" in names)
        bad = [t for t in tools if (t.get("function", {}).get("parameters") or {}).get("type") != "object"]
        check("schemas validos", not bad, str([t["function"]["name"] for t in bad]))
        # tipos: int params deben ser integer (bug from __future__ annotations)
        _by = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
        check("exec.timeout integer", _by.get("exec", {}).get("properties", {}).get("timeout", {}).get("type") == "integer")
        check("registry.current_images existe", hasattr(reg, "current_images"))
    except Exception as e:
        check("tools", False, str(e))

    section("5) FS / editor (toda la PC)")
    try:
        r1 = c.get(f"{BASE}/api/fs/list", params={"path": "."}).json()
        check("fs list proyecto", len(r1.get("entries") or []) > 0, f"{len(r1.get('entries') or [])}")
        r2 = c.get(f"{BASE}/api/fs/list", params={"path": ""}).json()
        drv = [e["name"] for e in (r2.get("entries") or [])]
        check("fs list unidades", len(drv) >= 1, str(drv))
        r3 = c.get(f"{BASE}/api/fs/list", params={"path": "C:\\"}).json()
        check("fs list C:\\", len(r3.get("entries") or []) > 0, f"{len(r3.get('entries') or [])}")
    except Exception as e:
        check("fs", False, str(e))

    section("6) Sessions CRUD")
    try:
        sid = c.post(f"{BASE}/api/sessions", json={"project": "diag"}).json()["id"]
        check("crear sesion", bool(sid))
        lst = c.get(f"{BASE}/api/sessions", params={"limit": 1000}).json()["sessions"]
        check("aparece en listado", any(s["id"] == sid for s in lst))
        d = c.delete(f"{BASE}/api/sessions/{sid}")
        check("borrar 200", d.status_code == 200)
        d2 = c.delete(f"{BASE}/api/sessions/{sid}")
        check("2do borrar 404", d2.status_code == 404)
    except Exception as e:
        check("sessions", False, str(e))

    section("7) Exec")
    try:
        r = c.post(f"{BASE}/api/exec", json={"command": "echo diag-ok", "cwd": "./workspace", "timeout": 10}).json()
        check("exec echo ok", r.get("ok") and "diag-ok" in (r.get("output") or ""), str(r.get("output"))[:60])
    except Exception as e:
        check("exec", False, str(e))

    section("7b) Endpoints nuevos (checkpoints / global search / catalog)")
    try:
        check("checkpoints GET", c.get(f"{BASE}/api/checkpoints", params={"session_id": "x"}).status_code == 200)
        check("global search GET", c.get(f"{BASE}/api/sessions/global/search", params={"q": "x"}).status_code == 200)
        check("catalog GET", c.get(f"{BASE}/api/sessions/catalog").status_code == 200)
        from src.core.diagnostics import diagnostics_for
        import pathlib
        p = "./workspace/_diag_ep.py"
        pathlib.Path(p).write_text("def f(:\n pass\n", encoding="utf-8")
        try:
            check("diagnostics-in-loop", "file_diagnostics" in diagnostics_for(p))
        finally:
            pathlib.Path(p).unlink(missing_ok=True)
    except Exception as e:
        check("endpoints nuevos", False, str(e))

    if RUN_LLM:
        section("8) LLM: tool-use (write_file/read_file)")
        try:
            body = {"message": "crea un archivo diag_e2e.txt en workspace con el texto 'DIAG_OK' y leelo", "model": "qwen3.5-9b"}
            j = c.post(f"{BASE}/api/chat", json=body, timeout=300).json()
            used = [s.get("tool_used") for s in (j.get("steps") or []) if s.get("tool_used")]
            check("LLM uso tools", any(t in ("write_file", "read_file") for t in used), str(used))
            check("LLM exito", j.get("success"), str(j.get("reply"))[:80])
        except Exception as e:
            check("LLM tool-use", False, str(e))

        section("9) LLM: vision (imagen)")
        try:
            png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PzWvWQAAAABJRU5ErkJggg==")
            att = c.post(f"{BASE}/api/upload", files={"file": ("t.png", io.BytesIO(png), "image/png")}).json()
            body = {"message": "describe esta imagen", "model": "gemma4",
                    "attachments": [{"id": att["id"], "name": att.get("name"), "mime": att.get("mime")}]}
            j = c.post(f"{BASE}/api/chat", json=body, timeout=300).json()
            check("vision respondio", j.get("success") and len((j.get("reply") or "")) > 5, str(j.get("reply"))[:80])
        except Exception as e:
            check("LLM vision", False, str(e))

        section("10) Streaming SSE (contenido final)")
        try:
            got_final = False
            final_text = ""
            with c.stream("POST", f"{BASE}/api/chat/stream", json={"message": "di exactamente: STREAM_OK", "model": "qwen3.5-9b"}, timeout=300) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    if "data:" in line:
                        got_final = True
                        final_text += line
            check("stream emitio eventos", got_final)
            check("stream contenido no vacio", len(final_text) > 20, f"{len(final_text)} chars")
        except Exception as e:
            check("streaming", False, str(e))

        section("11) Multi-turno (2 turnos, misma sesion)")
        try:
            sid = c.post(f"{BASE}/api/sessions", json={"project": "diag-multi"}).json()["id"]
            j1 = c.post(f"{BASE}/api/chat", json={"message": "di: UNO", "session_id": sid, "model": "qwen3.5-9b"}, timeout=300).json()
            check("turno 1 ok", j1.get("success") and "[Error" not in (j1.get("reply") or ""), str(j1.get("reply"))[:60])
            j2 = c.post(f"{BASE}/api/chat", json={"message": "ahora di: DOS", "session_id": sid, "model": "qwen3.5-9b"}, timeout=300).json()
            check("turno 2 ok (sin 500)", j2.get("success") and "[Error" not in (j2.get("reply") or ""), str(j2.get("reply"))[:60])
            c.delete(f"{BASE}/api/sessions/{sid}")
        except Exception as e:
            check("multi-turno", False, str(e))

        section("12) Auto + imagen (repetido)")
        try:
            png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PzWvWQAAAABJRU5ErkJggg==")
            for i in range(2):
                att = c.post(f"{BASE}/api/upload", files={"file": (f"a{i}.png", io.BytesIO(png), "image/png")}).json()
                body = {"message": "analiza esta imagen",
                        "attachments": [{"id": att["id"], "name": att.get("name"), "mime": att.get("mime")}]}
                j = c.post(f"{BASE}/api/chat", json=body, timeout=300).json()
                ok = j.get("success") and len((j.get("reply") or "")) > 5 and "[Error" not in (j.get("reply") or "")
                check(f"auto+imagen intento {i+1}", ok, str(j.get("reply"))[:70])
        except Exception as e:
            check("auto+imagen", False, str(e))

        section("13) Delegacion (gema_run) via LLM")
        try:
            body = {"message": "usa la herramienta gema_run con gema 'analyst' para responder cuanto es 2+2", "model": "qwen3.5-9b"}
            j = c.post(f"{BASE}/api/chat", json=body, timeout=300).json()
            used = [s.get("tool_used") for s in (j.get("steps") or []) if s.get("tool_used")]
            check("delego con gema_run", "gema_run" in used or "gema_list" in used, str(used))
            check("delegacion no crasheo", "[Error" not in (j.get("reply") or ""), str(j.get("reply"))[:70])
        except Exception as e:
            check("delegacion", False, str(e))

    section("14) Slash commands (sin LLM)")
    for cmd in ["/help", "/herramientas", "/alcance", "/modelo", "/permiso"]:
        try:
            j = c.post(f"{BASE}/api/slash", json={"command": cmd, "ctx": {}}, timeout=30).json()
            check(f"slash {cmd}", bool(j.get("message")) or j.get("ok") is not None, str(j.get("message"))[:60])
        except Exception as e:
            check(f"slash {cmd}", False, str(e))

    section("15) Scan sesiones recientes por errores")
    try:
        ss = c.get(f"{BASE}/api/sessions", params={"limit": 30}).json()["sessions"]
        errs = 0
        empties = 0
        for s in ss[:30]:
            msgs = c.get(f"{BASE}/api/sessions/{s['id']}/messages", params={"limit": 50}).json().get("messages", [])
            for m in msgs:
                ct = m.get("content") or ""
                if "[Error LLM" in ct or "todos los proveedores fallaron" in ct:
                    errs += 1
                if m.get("role") == "assistant" and ct.strip() == "":
                    empties += 1
        check("sin [Error LLM] en sesiones", errs == 0, f"{errs} errores")
        check("sin assistant vacios", empties == 0, f"{empties} vacios")
    except Exception as e:
        check("scan sesiones", False, str(e))

    c.close()

    section("RESUMEN")
    fails = [r for r in RESULTS if not r[1]]
    print(f"total={len(RESULTS)} pass={len(RESULTS)-len(fails)} fail={len(fails)}")
    for n, _ok, d in fails:
        print(f"  FAIL: {n} :: {d}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
