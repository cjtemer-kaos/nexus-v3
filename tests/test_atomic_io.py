"""atomic_io: o queda el archivo viejo, o el nuevo, nunca una mezcla."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.security.atomic_io import atomic_write_json, atomic_write_text


def _temporales(directorio: Path) -> list[Path]:
    return [p for p in directorio.iterdir() if p.name.endswith(".tmp")]


def test_texto_redondo(tmp_path):
    destino = tmp_path / "nota.txt"
    atomic_write_text(destino, "hola ñandú")
    assert destino.read_text(encoding="utf-8") == "hola ñandú"
    assert _temporales(tmp_path) == []


def test_json_redondo_y_legible(tmp_path):
    destino = tmp_path / "estado.json"
    atomic_write_json(destino, {"a": 1, "b": ["ñ", 2]})
    assert json.loads(destino.read_text(encoding="utf-8")) == {"a": 1, "b": ["ñ", 2]}
    assert "\n  " in destino.read_text(encoding="utf-8")
    assert _temporales(tmp_path) == []


def test_crea_directorios_intermedios(tmp_path):
    destino = tmp_path / "brain" / "sub" / "x.json"
    atomic_write_json(destino, {"ok": True})
    assert destino.exists()


def test_reemplaza_sin_dejar_el_viejo_a_medias(tmp_path):
    destino = tmp_path / "router_state.json"
    atomic_write_text(destino, '{"version": 1, "brazos": {"a": 1}}')
    atomic_write_text(destino, '{"version": 2}')
    assert json.loads(destino.read_text(encoding="utf-8")) == {"version": 2}


def test_payload_grande_no_se_trunca(tmp_path):
    destino = tmp_path / "grande.json"
    datos = {"filas": [{"i": i, "texto": "x" * 200} for i in range(5000)]}
    atomic_write_json(destino, datos)
    leido = json.loads(destino.read_text(encoding="utf-8"))
    assert len(leido["filas"]) == 5000
    assert leido == datos


def test_json_no_serializable_no_toca_el_destino(tmp_path):
    destino = tmp_path / "estado.json"
    atomic_write_text(destino, '{"version": 1}')
    with pytest.raises(TypeError):
        atomic_write_json(destino, {"malo": object()})
    assert destino.read_text(encoding="utf-8") == '{"version": 1}'
    assert _temporales(tmp_path) == []


def test_fallo_al_reemplazar_borra_el_temporal(tmp_path, monkeypatch):
    destino = tmp_path / "estado.json"
    atomic_write_text(destino, "viejo")

    def _explota(*a, **k):
        raise OSError("disco lleno")

    monkeypatch.setattr("src.security.atomic_io.os.replace", _explota)
    with pytest.raises(OSError):
        atomic_write_text(destino, "nuevo")
    assert destino.read_text(encoding="utf-8") == "viejo"
    assert _temporales(tmp_path) == []


def test_texto_no_str_falla_con_mensaje_claro(tmp_path):
    with pytest.raises(TypeError) as err:
        atomic_write_text(tmp_path / "x.txt", b"bytes")  # type: ignore[arg-type]
    assert "debe ser str" in str(err.value)


def test_sin_permiso_de_escritura_no_rompe_el_original(tmp_path):
    destino = tmp_path / "solo_lectura.json"
    atomic_write_json(destino, {"version": 1})
    os.chmod(destino, 0o444)
    try:
        atomic_write_json(destino, {"version": 2})
    except PermissionError:
        pass
    finally:
        os.chmod(destino, 0o644)
    assert json.loads(destino.read_text(encoding="utf-8"))["version"] in (1, 2)
