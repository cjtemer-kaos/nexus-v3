"""Cliente LSP mínimo (JSON-RPC por stdio, framing Content-Length).

Arranca un servidor de lenguaje (pylsp por defecto), abre el documento y devuelve
los diagnósticos publicados (semánticos: nombres indefinidos, imports sin usar, etc.),
no solo errores de sintaxis. Degrada con gracia si el servidor no está disponible.

Un cliente por raíz (cache) y un cliente por proceso; el servidor se apaga al salir.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import threading
import time

_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescriptreact", ".jsx": "javascriptreact",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
}


def language_id(path: str) -> str:
    return _LANG.get(pathlib.Path(path).suffix.lower(), "plaintext")


class LSPClient:
    def __init__(self, command: list[str], root: str, timeout: float = 6.0):
        self.command = command
        self.root = str(pathlib.Path(root).resolve())
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        self._diags: dict[str, list] = {}
        self._proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self.root,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._initialize()

    # -- framing --
    def _send(self, method: str, params: dict, is_request: bool = True):
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if is_request:
            self._id += 1
            msg["id"] = self._id
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            with self._lock:
                self._proc.stdin.write(header + body)
                self._proc.stdin.flush()
        except Exception:
            return None
        return msg.get("id")

    def _read_loop(self):
        f = self._proc.stdout
        if f is None:
            return
        while True:
            headers = {}
            while True:
                line = f.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    break
                key, _, val = line.partition(b":")
                headers[key.strip().lower()] = val.strip()
            try:
                n = int(headers.get(b"content-length", b"0"))
            except Exception:
                return
            body = f.read(n) if n else b""
            try:
                msg = json.loads(body)
            except Exception:
                continue
            if msg.get("method") == "textDocument/publishDiagnostics":
                p = msg.get("params") or {}
                self._diags[p.get("uri", "")] = p.get("diagnostics") or []

    def _initialize(self):
        self._send("initialize", {
            "processId": os.getpid(),
            "rootUri": pathlib.Path(self.root).as_uri(),
            "capabilities": {"textDocument": {"publishDiagnostics": {}}},
            "workspaceFolders": None,
        })
        self._send("initialized", {}, is_request=False)

    def diagnostics(self, path: str, text: str) -> list[dict]:
        uri = pathlib.Path(path).resolve().as_uri()
        self._diags.pop(uri, None)
        self._send("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": language_id(path), "version": 1, "text": text,
        }}, is_request=False)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if uri in self._diags:
                break
            time.sleep(0.1)
        return self._diags.get(uri, [])

    def close(self):
        try:
            self._send("shutdown", {})
            self._send("exit", {}, is_request=False)
        except Exception:
            pass
        try:
            self._proc.terminate()
        except Exception:
            pass


_CLIENTS: dict[str, LSPClient] = {}
_CLIENTS_LOCK = threading.Lock()


def get_client(root: str, command: list[str] | None = None) -> LSPClient | None:
    """Cliente cacheado por raíz. None si el servidor no arranca."""
    key = str(pathlib.Path(root).resolve())
    with _CLIENTS_LOCK:
        c = _CLIENTS.get(key)
        if c is not None and c._proc.poll() is None:
            return c
        try:
            c = LSPClient(command or ["python", "-m", "pylsp"], key)
        except Exception:
            return None
        _CLIENTS[key] = c
        return c


def format_diagnostics(diags: list[dict]) -> str:
    sev = {1: "error", 2: "warning", 3: "info", 4: "hint"}
    lines = []
    for d in diags[:20]:
        r = d.get("range", {}).get("start", {})
        line = int(r.get("line", 0)) + 1
        col = int(r.get("character", 0)) + 1
        lvl = sev.get(d.get("severity", 1), "error")
        msg = (d.get("message") or "").splitlines()[0]
        lines.append(f"{line}:{col} {lvl}: {msg}")
    return "\n".join(lines)
