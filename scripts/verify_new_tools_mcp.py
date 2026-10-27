"""Prueba en vivo: habla MCP por stdio con src/mcp_server.py y llama las tools nuevas.

No usa LLM: verifica que las tools nuevas estan expuestas y QUE RESPONDEN datos
reales (git_status sobre el repo del proyecto, repl con una celda con estado).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = r"D:\ias\proyectos\nexus-v3"
PY = os.path.join(ROOT, ".venv-ft314", "Scripts", "python.exe")


def main() -> int:
    proc = subprocess.Popen(
        [PY, "-m", "src.mcp_server"],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
    )
    next_id = 0

    def call(method: str, params: dict | None = None) -> dict:
        nonlocal next_id
        next_id += 1
        msg = {"jsonrpc": "2.0", "id": next_id, "method": method, "params": params or {}}
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        assert proc.stdout is not None
        line = proc.stdout.readline()
        return json.loads(line)

    try:
        init = call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "verificador", "version": "1"}})
        print("initialize ->", json.dumps(init.get("result", {}).get("serverInfo", {}), ensure_ascii=False))

        listed = call("tools/list")
        names = [t["name"] for t in listed.get("result", {}).get("tools", [])]
        nuevas = [n for n in names if n in ("git_status", "git_diff", "git_log", "git_commit",
                                            "run_tests", "repl")]
        print(f"tools totales={len(names)} nuevas={nuevas}")

        def tool(name: str, args: dict) -> dict:
            r = call("tools/call", {"name": name, "arguments": args})
            content = r.get("result", {}).get("content") or []
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            return {"isError": r.get("result", {}).get("isError"), "text": text, "raw": r}

        st = tool("git_status", {"cwd": ROOT})
        payload = json.loads(st["text"])
        print("git_status ->", json.dumps({k: payload[k] for k in ("branch", "head", "clean", "ahead", "behind")},
                                          ensure_ascii=False))

        df = tool("git_diff", {"cwd": ROOT, "stat": True})
        dpay = json.loads(df["text"])
        print("git_diff(stat) -> empty=", dpay["empty"], "chars=", len(dpay["diff"]))

        lg = tool("git_log", {"cwd": ROOT, "limit": 3})
        lpay = json.loads(lg["text"])
        print("git_log ->", [c["sha"] + " " + c["subject"][:40] for c in lpay["commits"]])

        r1 = tool("repl", {"code": "import sys\nvalor = 6 * 7"})
        r2 = tool("repl", {"code": "print(valor + 1)\nvalor"})
        p2 = json.loads(r2["text"])
        print("repl celda1 ok=", json.loads(r1["text"])["ok"],
              "| celda2 stdout=", repr(p2["stdout"]), "value=", p2["value"],
              "| kernel_pid=", (p2.get("kernel") or {}).get("pid"))

        rt = tool("run_tests", {"cwd": ROOT, "runner": "pytest",
                                "args": "tests/test_git_tools.py -q", "timeout": 300})
        rp = json.loads(rt["text"])
        print("run_tests -> ok=", rp["ok"], "exit=", rp["exit_code"],
              "counts=", rp["counts"], "failures=", len(rp["failures"]), "dur=", rp["duration_s"])

        ok = (all(n in names for n in ("git_status", "git_diff", "git_log", "git_commit", "run_tests", "repl"))
              and payload["head"] != "" and p2["value"] == "42" and rp["ok"] is True
              and rp["counts"].get("passed", 0) >= 13)
        print("VEREDICTO:", "OK" if ok else "FALLO")
        return 0 if ok else 1
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        err = proc.stderr.read() if proc.stderr else ""
        if err.strip():
            print("--- stderr del server ---")
            print(err[-1500:])


if __name__ == "__main__":
    sys.exit(main())
