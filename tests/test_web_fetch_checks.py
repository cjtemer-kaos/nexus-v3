"""web_fetch: no confundir "host inexistente" con "intento de SSRF".

Medido contra el registry real: `web_fetch` de `https://no-es-una-url` y de
`http://no-existe-xyz.invalid/` devolvia el mismo
`URL bloqueada (SSRF/metadata/privada)` que `http://127.0.0.1:9000/`. El modelo
concluye que el sitio esta bloqueado en vez de "la URL esta mal" y no reintenta.

Aparte: el cuerpo se decodificaba con `errors="replace"` y el charset declarado.
Cuando el servidor declara mal (o no declara) cada acento era un U+FFFD.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools.web_fetch import HTTPFetcher, _decode_web, is_safe_url, url_rejection  # noqa: E402


# --------------------------------------------------------------- motivos


def test_ip_publica_literal_es_usable():
    assert url_rejection("http://93.184.216.34/") == ""


def test_loopback_metadata_y_privadas_son_privada():
    for url in ("http://127.0.0.1:9000/", "http://localhost/",
                "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/",
                "http://192.168.1.16/", "http://metadata.google.internal/"):
        assert url_rejection(url) == "privada", url


def test_url_sin_esquema_o_sin_host_es_url_invalida():
    for url in ("no-es-una-url", "ftp://example.com/x", "https://", "//example.com"):
        assert url_rejection(url) == "url_invalida", url


def test_host_que_no_resuelve_es_dns_y_no_privada():
    """`.invalid` es un TLD reservado: nunca resuelve."""
    assert url_rejection("http://no-existe-xyz.invalid/") == "dns"


def test_is_safe_url_sigue_siendo_booleano():
    assert is_safe_url("http://127.0.0.1/") is False
    assert is_safe_url("http://93.184.216.34/") is True


# ------------------------------------------------------------- decodificacion


def test_charset_mentiroso_no_rompe_los_acentos():
    """Declara utf-8 y manda cp1252 (pasa todo el tiempo)."""
    raw = "cañón del sur".encode("cp1252")

    assert _decode_web(raw, "utf-8") == "cañón del sur"


def test_charset_declarado_se_respeta():
    raw = "cañón".encode("iso-8859-1")

    assert _decode_web(raw, "iso-8859-1") == "cañón"


def test_sin_charset_cae_en_utf8():
    assert _decode_web("ñandú".encode("utf-8"), "") == "ñandú"


def test_charset_desconocido_no_explota():
    assert _decode_web(b"hola", "x-no-existe") == "hola"


# ------------------------------------------------------------- HTTPFetcher


def test_httpfetcher_privada_dice_privada():
    res = HTTPFetcher("http://127.0.0.1:9000/api/health", timeout=2).fetch()

    assert res["reason"] == "privada"
    assert "restringido" in res["error"]


def test_httpfetcher_host_inexistente_dice_dns():
    res = HTTPFetcher("http://no-existe-xyz.invalid/", timeout=2).fetch()

    assert res["reason"] == "dns"
    assert "resolver" in res["error"].lower()
    assert "restringido" not in res["error"]


def test_httpfetcher_url_invalida_dice_url_invalida():
    res = HTTPFetcher("no-es-una-url", timeout=2).fetch()

    assert res["reason"] in ("dns", "url_invalida")
    assert "restringido" not in res["error"]


def test_httpfetcher_esquema_no_http_es_url_invalida():
    """`ftp://host/x` se prefijaba con `https://` y el host quedaba en `ftp`."""
    res = HTTPFetcher("ftp://example.com/x", timeout=2).fetch()

    assert res["reason"] == "url_invalida"
    assert res["error"].startswith("URL invalida")
    assert "resolver" not in res["error"]


# ------------------------------------------------------------ tool expuesta


@pytest.fixture()
def web_fetch_tool():
    import asyncio

    from src.api import server as S

    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, "test-webfetch", cfg)

    def usar(**args):
        return asyncio.run(reg.dispatch("web_fetch", args, scope=scope, hooks=None))

    return usar


def test_tool_privada_es_permission_error(web_fetch_tool):
    with pytest.raises(PermissionError) as e:
        web_fetch_tool(url="http://127.0.0.1:9000/api/health")

    assert "SSRF" in str(e.value)


def test_tool_host_inexistente_es_value_error_con_el_motivo(web_fetch_tool):
    with pytest.raises(ValueError) as e:
        web_fetch_tool(url="http://no-existe-xyz.invalid/")

    msg = str(e.value)
    assert "resolver" in msg.lower()
    assert "SSRF" not in msg
    assert msg.lower().count("resolver") == 1, msg


def test_tool_esquema_no_http_no_habla_de_dns(web_fetch_tool):
    with pytest.raises(ValueError) as e:
        web_fetch_tool(url="ftp://example.com/x")

    msg = str(e.value)
    assert "invalida" in msg.lower()
    assert "resolver" not in msg.lower()
