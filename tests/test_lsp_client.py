"""Tests del cliente LSP: framing JSON-RPC real contra un servidor falso por stdio."""
import sys

from src.core.lsp_client import LSPClient, format_diagnostics, get_client, language_id

FAKE_SERVER = r'''
import json, sys

def send(msg):
    body = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()

def read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, val = line.partition(b":")
        headers[key.strip().lower()] = val.strip()
    n = int(headers.get(b"content-length", b"0"))
    return json.loads(sys.stdin.buffer.read(n)) if n else None

while True:
    msg = read_msg()
    if msg is None:
        break
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}})
    elif method == "textDocument/didOpen":
        uri = msg["params"]["textDocument"]["uri"]
        send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": [{
                "range": {"start": {"line": 2, "character": 4}},
                "severity": 1,
                "message": "nombre indefinido: foo\nsegunda linea",
            }]},
        })
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
    elif method == "exit":
        break
'''


def test_language_id_por_extension():
    assert language_id("a/b/c.py") == "python"
    assert language_id("x.tsx") == "typescriptreact"
    assert language_id("x.jsx") == "javascriptreact"
    assert language_id("d.yaml") == "yaml"
    assert language_id("raro.zzz") == "plaintext"


def test_cliente_recibe_diagnosticos_por_stdio(tmp_path):
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")

    client = LSPClient([sys.executable, "-u", str(script)], root=str(tmp_path), timeout=10)
    try:
        diags = client.diagnostics(str(tmp_path / "modulo.py"), "foo = 1")
    finally:
        client.close()

    assert len(diags) == 1
    assert diags[0]["severity"] == 1
    assert diags[0]["range"]["start"]["line"] == 2


def test_format_diagnostics_usa_linea_y_columna_1_based():
    diags = [
        {"range": {"start": {"line": 0, "character": 0}}, "severity": 1,
         "message": "error real\ncon contexto extra"},
        {"range": {"start": {"line": 9, "character": 2}}, "severity": 2,
         "message": "import sin usar"},
    ]
    texto = format_diagnostics(diags)
    assert texto == "1:1 error: error real\n10:3 warning: import sin usar"


def test_format_diagnostics_tolera_campos_faltantes():
    assert format_diagnostics([{}]) == "1:1 error: "


def test_get_client_degrada_si_el_servidor_no_arranca(tmp_path):
    assert get_client(str(tmp_path), ["binario-que-no-existe-nexus-xyz"]) is None
