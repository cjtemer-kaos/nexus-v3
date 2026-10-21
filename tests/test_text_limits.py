"""Tests del recorte head+tail de salidas de tools (port de Cline output-limits)."""
from __future__ import annotations

from src.core.text_limits import truncate_head_tail


def test_corto_no_se_toca():
    text = "hola\nmundo\n"
    assert truncate_head_tail(text, 1000) == text


def test_largo_conserva_cabeza_y_cola():
    text = "INICIO\n" + ("x" * 50_000) + "\nFINAL: error en linea 42\n"
    out = truncate_head_tail(text, 1000, label="salida del comando")
    assert out.startswith("INICIO")
    assert out.endswith("FINAL: error en linea 42\n")
    assert "truncada" in out and "chars en total" in out
    assert len(out) <= 1000 + 300


def test_max_chars_minimo_no_explota():
    assert truncate_head_tail("abcdef", 1)
