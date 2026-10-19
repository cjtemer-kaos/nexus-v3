"""Tests de src/tools/web_fetch.py: politica SSRF y fallback urllib (HTTPFetcher)."""
import http.server
import threading

import pytest

from src.tools import web_fetch as wf


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://127.0.0.1:9100/mcp",
    "http://localhost/",
    "http://0.0.0.0/",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://100.100.100.200/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://metadata.google.internal/",
    "http://algo.local/",
    "http://caja.internal/",
    "file:///etc/passwd",
    "sin-esquema-ni-host",
])
def test_is_safe_url_bloquea_privadas(url):
    assert wf.is_safe_url(url) is False


def test_is_safe_url_permite_ip_publica():
    # IP literal: no hace falta DNS para el chequeo
    assert wf.is_safe_url("http://93.184.216.34/") is True


def test_html_a_texto_quita_script_y_entidades():
    html = "<html><head><title>Hola</title></head><body><script>alert(1)</script><p>Uno &amp; dos</p></body></html>"
    txt = wf._html_to_text(html)
    assert "Hola" in txt
    assert "Uno & dos" in txt
    assert "alert" not in txt
    assert "<p>" not in txt
    assert "&amp;" not in txt


def test_fetcher_bloquea_antes_de_la_red():
    r = wf.HTTPFetcher("http://127.0.0.1:9/", timeout=1).fetch()
    assert "error" in r and "success" not in r
    assert "restringido" in r["error"].lower()


class _Handler(http.server.BaseHTTPRequestHandler):
    resp_body: bytes = b""
    resp_status: int = 200
    resp_headers: dict = {}

    def do_GET(self):  # noqa: N802
        self.send_response(self.resp_status)
        for k, v in self.resp_headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(self.resp_body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def servidor():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def test_fetch_html_200_sin_escapar_markup(servidor, monkeypatch):
    _Handler.resp_status = 200
    _Handler.resp_headers = {"Content-Type": "text/html; charset=utf-8"}
    _Handler.resp_body = (
        "<html><head><title>Prueba</title></head>"
        "<body><script>alert(1)</script><p>Hola &amp; chau</p></body></html>"
    ).encode()
    monkeypatch.setattr(wf, "is_safe_url", lambda _u: True)  # permitir el localhost del test
    r = wf.HTTPFetcher(f"http://127.0.0.1:{servidor.server_port}/").fetch()
    assert r["success"] is True
    assert r["title"] == "Prueba"
    assert "Hola & chau" in r["content"]
    assert "alert" not in r["content"]
    assert "&lt;" not in r["content"]


def test_fetch_redirect_a_privada_se_bloquea(servidor, monkeypatch):
    _Handler.resp_status = 302
    _Handler.resp_headers = {"Location": "http://127.0.0.1:9/privado"}
    _Handler.resp_body = b""
    real = wf.is_safe_url
    permitido = f"http://127.0.0.1:{servidor.server_port}"
    monkeypatch.setattr(wf, "is_safe_url", lambda u: True if u.startswith(permitido) else real(u))
    r = wf.HTTPFetcher(f"http://127.0.0.1:{servidor.server_port}/").fetch()
    assert "error" in r and "restringido" in r["error"].lower()
    assert r["url"].endswith("/privado")


def test_fetch_http_error_no_lanza(servidor, monkeypatch):
    _Handler.resp_status = 404
    _Handler.resp_headers = {"Content-Type": "text/plain"}
    _Handler.resp_body = b"nope"
    monkeypatch.setattr(wf, "is_safe_url", lambda _u: True)
    r = wf.HTTPFetcher(f"http://127.0.0.1:{servidor.server_port}/x").fetch()
    assert r["error"] == "HTTP 404"
    assert r["status_code"] == 404
