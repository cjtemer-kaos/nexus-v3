"""`read_file` del agente: el aviso de recorte y el rango fuera del archivo.

Dos defectos medidos leyendo un archivo de 5000 lineas (218 KB):

1. **El aviso de recorte mandaba a la herramienta equivocada.** El read por
   defecto entrega 48 229 chars (~12k tokens) con el aviso generico de
   `truncate_head_tail`: *"Refina el comando (grep/head/tail) para ver el medio
   elidido"*. Ese consejo es para la salida de un comando; aca el modelo ya
   tiene el archivo leido y lo unico que puede hacer es paginar con
   `offset`/`limit`. Ahora el aviso lo dice, con el total de lineas.

2. **Un offset fuera de rango devolvia un rango invertido y mudo.** Con
   `offset=9999` en un archivo de 5000 lineas la respuesta era exactamente
   `"[<path>: líneas 10000-9999 de 5000]"`: sin contenido, sin decir que el
   offset no existe. Un modelo confundido repite el mismo offset y el anti-loop
   lo corta. Ahora dice el rango valido.

Un archivo vacio devolvia `""` (indistinguible de una tool que no respondio);
ahora dice que esta vacio.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from src.core.text_limits import truncate_head_tail  # noqa: E402


@pytest.fixture()
def leer(tmp_path):
    from src.api import server as S

    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, "test-read", cfg)

    def _leer(path, **args):
        raw = asyncio.run(reg.dispatch("read_file", {"path": str(path), **args},
                                       scope=scope, hooks=None))
        return raw if isinstance(raw, str) else getattr(raw, "output", str(raw))

    return _leer


def _archivo(tmp_path, lineas: int = 5000):
    f = tmp_path / "grande.py"
    f.write_text("\n".join(f"linea_{i:05d} = {i}  # {'x' * 20}" for i in range(lineas)) + "\n",
                 encoding="utf-8")
    return f


def test_lectura_completa_grande_ensena_a_paginar(tmp_path, leer):
    f = _archivo(tmp_path)
    out = leer(f)

    assert "truncada" in out
    assert "offset" in out and "limit" in out, "el aviso tiene que decir como paginar"
    assert "5000" in out, "el aviso tiene que dar el total de lineas"
    assert "grep/head/tail" not in out, "no aplica: el archivo ya esta leido"


def test_lectura_paginada_sigue_diciendo_el_offset_siguiente(tmp_path, leer):
    f = _archivo(tmp_path)
    out = leer(f, offset=100, limit=50)

    cabecera = out.splitlines()[0]
    assert "líneas 101-150 de 5000" in cabecera
    assert "seguí con offset=150" in cabecera
    assert out.splitlines()[1].startswith("linea_00100")


def test_offset_mas_alla_del_final_es_explicito(tmp_path, leer):
    f = _archivo(tmp_path)
    out = leer(f, offset=9999, limit=10)

    assert "mas alla del final" in out
    assert "9999" in out
    assert "0 y 4999" in out
    assert "10000-9999" not in out, "no puede devolver un rango invertido"


def test_offset_en_el_borde_funciona(tmp_path, leer):
    f = _archivo(tmp_path, lineas=10)
    out = leer(f, offset=9, limit=5)

    assert "líneas 10-10 de 10" in out
    assert "linea_00009" in out


def test_archivo_vacio_lo_dice(tmp_path, leer):
    f = tmp_path / "vacio.txt"
    f.write_text("", encoding="utf-8")

    assert "vacio" in leer(f)
    assert "vacio" in leer(f, offset=0, limit=10)


def test_limit_negativo_equivale_a_leer_todo(tmp_path, leer):
    """Contrato documentado: limit<=0 = hasta el final."""
    f = _archivo(tmp_path, lineas=100)
    completo = leer(f, offset=0, limit=0)
    negativo = leer(f, offset=0, limit=-7)

    assert completo == negativo


def test_truncate_head_tail_acepta_un_consejo_propio():
    texto = "a" * 5000
    out = truncate_head_tail(texto, 1000, label="x", hint="usa otra cosa")
    assert "usa otra cosa" in out
    assert "grep/head/tail" not in out

    # sin hint, el consejo por defecto no cambia (lo usan exec y diff)
    out2 = truncate_head_tail(texto, 1000, label="salida del comando")
    assert "grep/head/tail" in out2
