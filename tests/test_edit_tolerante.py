"""Edición tolerante: el modelo copia mal un fragmento y Nexus no se traba.

El bucle real de la corrida de eMule era: `edit` no encuentra el texto (un
espacio de más, la sangría corrida o un `\\n` literal), el modelo relee el
archivo, vuelve a mandar el mismo fragmento mal copiado, y así gasta rondas
enteras en un problema de un carácter. opencode resuelve eso con una cadena de
estrategias de reemplazo; acá se porta lo mismo, con dos reglas:

1. Solo se aplica si la coincidencia es **única** (nunca se adivina entre dos).
2. Si no hay coincidencia, el error trae el bloque real con números de línea.

Y el gate de entrega escala de modelo cuando el mismo modelo repite el enfoque.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from src.api import server as S
from src.core.fuzzy_replace import (
    bloque_mas_parecido,
    ocurrencias_lineas,
    reemplazo_tolerante,
)
from src.core.scope import Scope, ScopeConfig

ORIG = "def total(a, b):\n    suma = a + b\n    return suma\n"


@pytest.fixture()
def tools(tmp_path):
    (tmp_path / "calc.py").write_text(ORIG, encoding="utf-8")
    cfg = S._load_config_safe()
    scope = Scope(ScopeConfig(allowed_roots=(str(tmp_path),), permission="full"),
                  project_root=tmp_path)
    reg = S._build_registry(scope, f"test-fuzzy-{uuid.uuid4().hex[:8]}", cfg)

    def llamar(name, args):
        return asyncio.run(reg.dispatch(name, args, scope=scope, hooks=None))

    return llamar, tmp_path


# --------------------------------------------------------------- estrategias


def test_sangria_corrida_se_aplica_y_queda_bien_indentada(tools):
    llamar, raiz = tools
    # El modelo copia el bloque entero sin la sangría de 4 espacios.
    out = llamar("edit", {"path": "calc.py",
                          "old": "def total(a, b):\nsuma = a + b\nreturn suma",
                          "new": "def total(a, b):\nsuma = a * b\nreturn suma"})
    texto = (raiz / "calc.py").read_text(encoding="utf-8")
    assert "suma = a * b" in texto
    assert linea_indent(texto, "suma = a * b") == 4, texto
    assert "coincidencia aproximada" in out


def test_espacios_de_mas_se_aplican(tools):
    llamar, raiz = tools
    llamar("edit", {"path": "calc.py",
                    "old": "    suma  =  a + b\n",
                    "new": "    suma = a + b  # ok\n"})
    assert "# ok" in (raiz / "calc.py").read_text(encoding="utf-8")


def test_escapes_literales_del_modelo_se_aplican(tools):
    llamar, raiz = tools
    llamar("edit", {"path": "calc.py",
                    "old": "    suma = a + b\\n    return suma",
                    "new": "    suma = a + b\\n    return suma * 2"})
    texto = (raiz / "calc.py").read_text(encoding="utf-8")
    assert "return suma * 2" in texto
    assert "\\n" not in texto  # no se escribió el escape literal


def test_archivo_con_tabs_conserva_tabs(tmp_path):
    p = tmp_path / "tabs.py"
    p.write_text("def f():\n\ttotal = 1\n\treturn total\n", encoding="utf-8")
    texto, estrategia = reemplazo_tolerante(p.read_text(encoding="utf-8"),
                                            "    total = 1", "    total = 2")
    assert estrategia
    assert "\ttotal = 2" in texto
    assert " total = 2\n" not in texto.replace("\ttotal = 2", "")


def test_no_inventa_reemplazo_si_el_texto_no_esta(tools):
    llamar, raiz = tools
    antes = (raiz / "calc.py").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        llamar("edit", {"path": "calc.py",
                        "old": "    producto = a * b", "new": "    producto = 0"})
    assert (raiz / "calc.py").read_text(encoding="utf-8") == antes


def test_ambiguo_no_se_aplica_y_dice_en_que_lineas(tools):
    llamar, raiz = tools
    (raiz / "dup.py").write_text("x = 1\ny = 0\nx = 1\n", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        llamar("edit", {"path": "dup.py", "old": "x = 1", "new": "x = 2"})
    msg = str(e.value)
    assert "2 veces" in msg and "1, 3" in msg


def test_crlf_se_preserva_con_reemplazo_tolerante(tmp_path):
    p = tmp_path / "w.ini"
    p.write_bytes(b"clave  =  uno\r\nclave2=dos\r\n")
    texto, _ = reemplazo_tolerante(p.read_text(encoding="utf-8"),
                                   "clave = uno", "clave=tres")
    assert "\r\n" not in texto  # el modulo trabaja en LF; server re-convierte
    assert "clave=tres" in texto and "clave2=dos" in texto


def test_bloque_anclado_aplica_si_primera_y_ultima_coinciden(tools):
    """Caso tipico: el modelo reescribe una funcion entera y se equivoca en una
    linea del medio. Antes era "no encontrado"; ahora reemplaza el bloque."""
    llamar, raiz = tools
    out = llamar("edit", {"path": "calc.py",
                          "old": "def total(a, b):\n    suma = a + c\n    return suma",
                          "new": "def total(a, b):\n    suma = a + b\n    return suma * 2"})
    texto = (raiz / "calc.py").read_text(encoding="utf-8")
    assert "return suma * 2" in texto
    assert "bloque anclado" in out


def test_bloque_anclado_no_pisa_otro_bloque_del_medio(tmp_path):
    """Si el cuerpo no se parece (>=0.55), no se toca: mejor fallar que
    sobrescribir codigo que el modelo no estaba mirando."""
    p = tmp_path / "otro.py"
    p.write_text("def total():\n    print('hola')\n    return 1\n", encoding="utf-8")
    r = reemplazo_tolerante(p.read_text(encoding="utf-8"),
                            "def total():\n    raise RuntimeError('nada que ver aca')\n    return 1",
                            "def total():\n    return 2")
    assert r is None


# ------------------------------------------------------------ cuando falla


def test_el_error_trae_el_bloque_real_con_lineas(tools):
    llamar, _ = tools
    with pytest.raises(ValueError) as e:
        llamar("edit", {"path": "calc.py",
                        "old": "def suma_total(a, b):\n    suma = a + c\n    return suma",
                        "new": "x"})
    msg = str(e.value)
    assert "más parecido" in msg
    assert "2 |" in msg and "suma = a + b" in msg
    assert "buscabas" in msg and "a + c" in msg


def test_el_error_sin_parecido_no_miente(tools):
    llamar, _ = tools
    with pytest.raises(ValueError) as e:
        llamar("edit", {"path": "calc.py",
                        "old": "import numpy as np\nnp.zeros(3)", "new": "x"})
    assert "no está ahí" in str(e.value) or "más parecido" in str(e.value)


def test_bloque_mas_parecido_sobre_texto_real():
    pista = bloque_mas_parecido(ORIG, "    suma = a - b")
    assert "suma = a + b" in pista
    assert "línea" in pista


def test_ocurrencias_lineas_es_1_based():
    assert ocurrencias_lineas("uno\ndos\nuno\n", "uno") == [1, 3]


# ------------------------------------------------- escalado de modelo (gate)


def test_escalada_usa_la_config():
    cfg = {"delivery": {"escalate_after": 1, "escalate_models": ["grande"]}}
    assert S._escalada_de_modelos(cfg, {}) == (1, ["grande"])


def test_escalada_el_body_pisa_la_config():
    cfg = {"delivery": {"escalate_after": 3, "escalate_models": ["a"]}}
    desde, modelos = S._escalada_de_modelos(cfg, {"escalate_after": 2,
                                                  "escalate_models": "x, y"})
    assert desde == 2 and modelos == ["x", "y"]


def test_escalada_sin_config_usa_el_default():
    desde, modelos = S._escalada_de_modelos({}, {})
    assert desde >= 1 and modelos and all(isinstance(m, str) for m in modelos)


def linea_indent(texto: str, aguja: str) -> int:
    for ln in texto.splitlines():
        if aguja in ln:
            return len(ln) - len(ln.lstrip())
    return -1
