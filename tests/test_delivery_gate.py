"""Gate de entrega: el pedido se cierra por comportamiento, no por relato.

Nexus declaraba "LISTO:" con el programa a medio hacer (llego a abrir el eMule
original y llamarlo referencia en vez de terminar el suyo). La ronda se mide con
un comando real: si falla, se vuelve a entrar con su salida como evidencia.
"""

from __future__ import annotations

import sys

from src.api.server import _check_entrega_sync, _texto_evidencia


def _py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def test_check_que_pasa_devuelve_ok_y_su_salida():
    ok, salida = _check_entrega_sync(_py("print('todo verde')"), None, 60)
    assert ok is True
    assert "todo verde" in salida


def test_check_que_falla_devuelve_el_codigo_real():
    ok, salida = _check_entrega_sync(
        _py("import sys; print('falta el buscador'); sys.exit(3)"), None, 60)
    assert ok is False
    assert "falta el buscador" in salida


def test_check_que_no_termina_no_cuelga_la_ronda():
    ok, salida = _check_entrega_sync(_py("import time; time.sleep(30)"), None, 2)
    assert ok is False
    assert "no termino" in salida


def test_check_inexistente_no_rompe_y_reporta():
    ok, salida = _check_entrega_sync("comando-que-no-existe-xyz --flag", None, 30)
    assert ok is False
    assert salida  # el shell siempre dice algo


def test_cwd_se_respeta(tmp_path):
    (tmp_path / "marca.txt").write_text("x", encoding="utf-8")
    ok, _ = _check_entrega_sync("dir marca.txt" if sys.platform == "win32" else "ls marca.txt",
                                str(tmp_path), 30)
    assert ok is True


def test_la_evidencia_lleva_la_salida_cruda_sin_pistas():
    texto = _texto_evidencia("python aceptacion.py", "FAIL chequeo 5: no hay buscador")
    assert "python aceptacion.py" in texto
    assert "FAIL chequeo 5: no hay buscador" in texto
    assert "FALLA" in texto
    # No debe dictar pasos: solo el resultado real.
    assert "paso 1" not in texto.lower()
