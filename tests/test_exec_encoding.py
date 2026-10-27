"""Salida de procesos: code page OEM (Windows) y exit code visible.

Dos defectos medidos con la tool `exec` real:

1. **Acentos de `cmd` llegaban como U+FFFD.** `echo cañón del sur ñandú` por
   `cmd.exe` devuelve `b'ca\\xa4\\xa2n del sur \\xa4and\\xa3'` (code page OEM 437
   en esta maquina; 850 en un Windows en espanol). Se decodificaba como
   `utf-8 + errors="replace"` -> `ca\\ufffd\\ufffdn del sur \\ufffdand\\ufffd`.
   Un script de Python, en cambio, escribe UTF-8. O sea: hay que DETECTAR, no
   fijar.

2. **El exit code desaparecia cuando habia salida.** `exec` solo agregaba
   `[exit N]` si la salida estaba vacia. El caso normal de falla (un script que
   loguea y devuelve 1, `git` fuera de un repo) llegaba al modelo como si
   hubiera funcionado.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.file_text import decode_output  # noqa: E402
from src.core.jobs import _decode_tramo  # noqa: E402


# ------------------------------------------------------------- decode_output


def test_bytes_de_cmd_cp437_se_decodifican_bien():
    """Estos son los bytes reales de `echo cañón del sur ñandú` en cmd.exe."""
    raw = b"ca\xa4\xa2n del sur \xa4and\xa3\r\n"

    texto, enc = decode_output(raw)

    assert texto.strip() == "cañón del sur ñandú"
    assert "\ufffd" not in texto
    assert enc.startswith("cp"), enc


def test_utf8_de_python_se_decodifica_como_utf8():
    texto, enc = decode_output("cañón ñandú".encode("utf-8"))

    assert texto == "cañón ñandú"
    assert enc == "utf-8"


def test_salida_vacia():
    assert decode_output(b"") == ("", "utf-8")


def test_ascii_no_se_rompe():
    assert decode_output(b"hola mundo\r\n")[0] == "hola mundo\r\n"


# ---------------------------------------------------------------- jobs


def test_job_decode_no_deja_mojibake_en_salida_de_consola():
    texto, consumidos = _decode_tramo(b"ca\xa4\xa2n del sur\r\n")

    assert "cañón" in texto
    assert "\ufffd" not in texto
    assert consumidos == len(b"ca\xa4\xa2n del sur\r\n")


def test_job_decode_sigue_cortando_en_limite_de_caracter_utf8():
    """Un `á` partido entre dos tramos no puede convertirse en U+FFFD."""
    texto, consumidos = _decode_tramo("hola á".encode("utf-8")[:-1])

    assert texto == "hola "
    assert consumidos == len("hola ".encode("utf-8"))


# ------------------------------------------------------------------ exec


@pytest.fixture()
def exec_tool():
    from src.api import server as S

    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, "test-exec", cfg)

    def correr(**args):
        return asyncio.run(reg.dispatch("exec", args, scope=scope, hooks=None))

    return correr


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_exec_muestra_el_exit_code_aunque_haya_salida(exec_tool):
    out = exec_tool(command='cmd /c "echo fallo controlado & exit 5"')

    assert "fallo controlado" in out
    assert "[exit 5]" in out, out


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_exec_sin_salida_sigue_mostrando_el_exit_code(exec_tool):
    assert exec_tool(command="cmd /c exit 3").strip() == "[exit 3]"


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_exec_no_devuelve_mojibake_en_los_acentos(exec_tool):
    out = exec_tool(command="echo cañón del sur ñandú")

    assert "cañón del sur ñandú" in out
    assert "\ufffd" not in out


def test_exec_de_python_utf8_sigue_bien(exec_tool):
    out = exec_tool(command="""python -c "print('cañón ñandú')" """.strip())

    assert "cañón ñandú" in out
    assert "\ufffd" not in out


def test_exito_no_lleva_etiqueta_de_exit(exec_tool):
    out = exec_tool(command="echo ok")

    assert out.strip() == "ok"
    assert "[exit" not in out
