"""El jail de exec no puede ser la puerta de atras de los secretos.

Hallazgo real: con `permission: full`, `/api/fs/read?path=.env` respondia 403
pero `POST /api/exec {"command": "type .env"}` devolvia el contenido, porque
`_exec_jail` se desactivaba entero en 'full' y nadie miraba los deny patterns.
"""
from pathlib import Path

import pytest

from src.api.server import (
    _exec_jail,
    _exec_permission_error,
    _secret_path_in_command,
)


def _scope(tmp_path, **scope_cfg):
    from src.api.server import _build_scope

    cfg = {"scope": {"allowed_roots": [str(tmp_path)], **scope_cfg}}
    return _build_scope(cfg)


@pytest.fixture()
def secret_env(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / ".env").write_text("SECRETO=1\n", encoding="utf-8")
    (d / "README.md").write_text("hola\n", encoding="utf-8")
    (d / "claves").mkdir()
    (d / "claves" / "server.key").write_text("KEY\n", encoding="utf-8")
    return d


@pytest.mark.parametrize("cmd", [
    "type .env",
    "cat .env",
    "Get-Content .env",
    "powershell -c Get-Content .env",
    "copy .env copia.txt",
    "cat claves/server.key",
    "cat ./.env.local",
    "type %USERPROFILE%\\.ssh\\id_rsa",
])
def test_comando_que_toca_secretos_se_bloquea_incluso_en_full(secret_env, cmd):
    scope = _scope(secret_env, permission="full")
    assert scope.permission == "full"
    hit = _secret_path_in_command(cmd, secret_env, scope)
    assert hit is not None, f"{cmd!r} deberia referenciar un archivo protegido"
    assert ".env" in hit or "id_rsa" in hit or ".key" in hit


@pytest.mark.parametrize("cmd", [
    "echo hola",
    "git status",
    "python -m pytest -q",
    "cat README.md",
    "dir",
    "ls -la",
])
def test_comandos_normales_no_se_bloquean(secret_env, cmd):
    scope = _scope(secret_env, permission="full")
    assert _secret_path_in_command(cmd, secret_env, scope) is None
    assert _exec_permission_error(cmd, secret_env, scope) is None


def test_ruta_absoluta_a_secreto_se_bloquea(secret_env):
    scope = _scope(secret_env, permission="full")
    target = str(secret_env / ".env")
    assert _secret_path_in_command(f"type {target}", secret_env, scope) is not None


def test_escritura_en_full_no_depende_de_la_config_estatica(secret_env):
    """El jail sigue al permiso EFECTIVO del turno, no a `cfg.permission`.

    Config en 'full' pero turno degradado a read_only: antes `_exec_jail` leia
    `scope.cfg.permission` y devolvia None, dejando pasar encadenamiento.
    """
    scope = _scope(secret_env, permission="full")
    assert _exec_jail("echo a; echo b", secret_env, scope) is None

    scope.runtime_permission = "read_only"
    assert scope.permission == "read_only"
    assert _exec_jail("echo a; echo b", secret_env, scope) is not None


def test_exec_permission_error_reporta_motivo(secret_env):
    scope = _scope(secret_env, permission="full")
    motivo = _exec_permission_error("type .env", secret_env, scope)
    assert motivo and "protegido" in motivo

    motivo_jail = _exec_permission_error("echo a && echo b", secret_env, scope)
    assert motivo_jail is None  # full permite shell completa en comandos inocuos

    scope.runtime_permission = "read_only"
    assert _exec_permission_error("echo a && echo b", secret_env, scope) is not None


def test_sin_deny_patterns_no_hay_falsos_positivos(tmp_path):
    import dataclasses

    scope = _scope(tmp_path, permission="full")
    scope.cfg = dataclasses.replace(scope.cfg, deny_patterns=())
    assert _secret_path_in_command("type .env", tmp_path, scope) is None


def test_path_tokens_absurdamente_largos_no_rompen(secret_env):
    scope = _scope(secret_env, permission="full")
    assert _secret_path_in_command("echo " + "a" * 5000, secret_env, scope) is None


def test_decode_process_bytes_conserva_utf8():
    """Con `text=True`, cp1252 reventaba con UTF-8 y la salida se perdia (None)."""
    from src.api.server import _decode_process_bytes

    texto = "README con acentos: áéíóú — ñ 日本語"
    raw = texto.encode("utf-8")
    assert _decode_process_bytes(raw) == texto
    assert _decode_process_bytes(None) == ""
    assert _decode_process_bytes("ya es str") == "ya es str"


def test_decode_process_bytes_no_revienta_con_bytes_invalidos():
    from src.api.server import _decode_process_bytes

    out = _decode_process_bytes(b"\x9d\xff\xfe ok")
    assert "ok" in out
    assert isinstance(out, str)


def test_decode_process_bytes_normaliza_crlf():
    from src.api.server import _decode_process_bytes

    assert _decode_process_bytes(b"a\r\nb\r\n") == "a\nb\n"


def test_exec_http_no_usa_text_true():
    """Regresion: `text=True` decodifica en cp1252 y devuelve stdout=None."""
    import inspect
    import re

    from src.api.server import exec_cmd

    src = inspect.getsource(exec_cmd)
    bloque = src[src.find("subprocess.run"):src.find("capture_output")]
    assert "text=True" not in bloque.replace(" ", "")
    assert re.search(r"_decode_process_bytes", src)

