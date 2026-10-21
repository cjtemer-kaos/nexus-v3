"""Tests del recorte head+tail de salidas de tools (port de Cline output-limits)."""
from __future__ import annotations

import json

from src.core.text_limits import cap_json_list, truncate_head_tail


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

def test_cap_json_list_conserva_json_valido():
    payload = {"matches": [f"src/archivo_{i}.py" for i in range(2000)], "pattern": "*.py"}
    out = cap_json_list(payload, "matches", 5000, hint="afina el patron")
    data = json.loads(out)
    assert data["truncated"] is True
    assert data["hint"] == "afina el patron"
    assert len(data["matches"]) < 2000
    assert len(data["matches"]) > 0
    assert len(out) <= 5000


def test_cap_json_list_corto_no_cambia():
    payload = {"entries": ["a", "b"], "truncated": False}
    out = cap_json_list(payload, "entries", 5000)
    assert json.loads(out) == payload
