"""REPL de Python persistente: una sola tool con estado entre llamadas.

Idea tomada de Prime Agent (RLM): en vez de encadenar `exec` con scripts sueltos
—donde cada llamada pierde imports, variables y datos cargados— el modelo habla
con un unico kernel que vive mientras dure la sesion. Asi puede cargar un CSV
una vez, inspeccionarlo en varias celdas y recien despues calcular.

Reglas de seguridad y de vida:

- El kernel corre con el mismo usuario y permisos que `exec`: la tool es mutante
  y pasa por `scope.check_tool`, igual que la shell. No es un sandbox nuevo.
- Una celda que no responde en `timeout_s` NO bloquea el turno: se mata el
  proceso, se avisa que el estado se perdio y la proxima celda arranca limpia.
- La salida se recorta en el hijo (200 KB) y otra vez en el padre, para que un
  `print` en loop no llene el contexto.
- Un kernel por sesion y por proceso; se apaga solo tras `idle_s` sin uso.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

MARKER = "\x1eNEXUS_REPL\x1e"
CHILD_OUTPUT_CAP = 200_000
IDLE_DEFAULT_S = 1800.0
TIMEOUT_DEFAULT_S = 30.0

_BOOTSTRAP = r'''
import io, json, sys, time, traceback

MARKER = "\x1eNEXUS_REPL\x1e"
CAP = %(cap)d
ns = {"__name__": "__main__", "__builtins__": __builtins__}

def _reply(payload):
    sys.stdout.write(MARKER + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        msg = json.loads(raw)
    except Exception:
        continue
    cid = msg.get("id")
    code = msg.get("code") or ""
    out, err, value_repr, exc = io.StringIO(), io.StringIO(), None, None
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    started = time.time()
    try:
        import ast
        tree = ast.parse(code, "<repl>", "exec")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body.pop().value)
        exec(compile(tree, "<repl>", "exec"), ns, ns)
        if last is not None:
            val = eval(compile(last, "<repl>", "eval"), ns, ns)
            ns["_"] = val
            value_repr = repr(val)
    except SystemExit as e:
        exc = "SystemExit: %%s" %% (e.code,)
    except BaseException as e:
        exc = traceback.format_exc(limit=8)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    stdout, stderr = out.getvalue(), err.getvalue()
    _reply({
        "id": cid,
        "ok": exc is None,
        "stdout": stdout[:CAP],
        "stderr": stderr[:CAP],
        "value": value_repr[:CAP] if value_repr is not None else None,
        "error": exc[:CAP] if exc else None,
        "duration_ms": int((time.time() - started) * 1000),
    })
'''


class ReplError(RuntimeError):
    pass


class ReplKernel:
    """Un proceso Python con namespace propio y protocolo JSON por stdin/stdout."""

    def __init__(self, cwd: Path, timeout_s: float = TIMEOUT_DEFAULT_S):
        self.cwd = Path(cwd)
        self.timeout_s = max(1.0, float(timeout_s))
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._counter = 0
        self.last_used = time.time()
        self.cells = 0
        self._started_at = 0.0

    # ---- proceso ----
    def _start(self) -> None:
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        self._proc = subprocess.Popen(
            [sys.executable, "-u", "-c", _BOOTSTRAP % {"cap": CHILD_OUTPUT_CAP}],
            cwd=str(self.cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=env, creationflags=creation,
        )
        self._started_at = time.time()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def kill(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass

    def _ensure(self) -> None:
        if not self.alive:
            self.kill()
            self._start()
            self.cells = 0

    # ---- ejecucion ----
    def run(self, code: str, timeout_s: float | None = None) -> dict[str, Any]:
        """Ejecuta una celda y devuelve stdout/stderr/valor/error."""
        if not isinstance(code, str) or not code.strip():
            raise ReplError("code requerido (celda de Python no vacía)")
        timeout = max(1.0, float(timeout_s or self.timeout_s))
        with self._lock:
            reiniciado = False
            if not self.alive:
                self._ensure()
                reiniciado = True
            self._counter += 1
            cid = self._counter
            proc = self._proc
            assert proc is not None and proc.stdin and proc.stdout
            try:
                proc.stdin.write(json.dumps({"id": cid, "code": code}, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception:
                self.kill()
                self._ensure()
                reiniciado = True
                proc = self._proc
                assert proc is not None and proc.stdin
                proc.stdin.write(json.dumps({"id": cid, "code": code}, ensure_ascii=False) + "\n")
                proc.stdin.flush()

            line = self._read_reply(proc, timeout)
            if line is None:
                self.kill()
                return {
                    "ok": False,
                    "timed_out": True,
                    "error": (f"la celda no terminó en {timeout:.0f}s; maté el kernel y el "
                              f"estado se perdió (la próxima celda arranca limpia)"),
                    "stdout": "", "stderr": "", "value": None,
                    "cell": cid, "restarted": True,
                }
            self.cells += 1
            self.last_used = time.time()
            out = json.loads(line[len(MARKER):])
            out["cell"] = cid
            out["restarted"] = reiniciado
            out["kernel_age_s"] = round(time.time() - self._started_at, 1)
            return out

    def _read_reply(self, proc: subprocess.Popen[str], timeout: float) -> str | None:
        """Espera la linea marcada, ignorando lo que el codigo escriba directo a fd1."""
        q: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    if raw.startswith(MARKER):
                        q.put(raw.rstrip("\n"))
                        return
            except Exception:
                pass
            q.put(None)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None

    def info(self) -> dict[str, Any]:
        return {
            "alive": self.alive,
            "cells": self.cells,
            "pid": self._proc.pid if self._proc else None,
            "cwd": str(self.cwd),
            "idle_s": round(time.time() - self.last_used, 1) if self.last_used else 0.0,
        }


_KERNELS: dict[str, ReplKernel] = {}
_KERNELS_LOCK = threading.Lock()


def get_kernel(key: str, cwd: Path, timeout_s: float = TIMEOUT_DEFAULT_S,
               idle_s: float = IDLE_DEFAULT_S) -> ReplKernel:
    """Kernel de la sesion `key`, reciclando si quedo ocioso."""
    with _KERNELS_LOCK:
        k = _KERNELS.get(key)
        if k is not None and k.alive and idle_s > 0 and (time.time() - k.last_used) > idle_s:
            k.kill()
            k = None
        if k is None:
            k = ReplKernel(cwd, timeout_s=timeout_s)
            _KERNELS[key] = k
        return k


def drop_kernel(key: str) -> bool:
    with _KERNELS_LOCK:
        k = _KERNELS.pop(key, None)
    if k is None:
        return False
    k.kill()
    return True


def shutdown_all() -> None:
    with _KERNELS_LOCK:
        kernels = list(_KERNELS.values())
        _KERNELS.clear()
    for k in kernels:
        k.kill()
