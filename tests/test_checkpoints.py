"""Checkpoints + diff: undo de las mutaciones del agente.

Cubre snapshot(index+copia), restore, diff contra el archivo actual y el
caso de dos snapshots en el mismo segundo (no deben colisionar).
"""
from __future__ import annotations

import pytest

from src.core import checkpoints as cp
from src.core.diffutil import checkpoint_diff, unified


@pytest.fixture(autouse=True)
def _cp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "_ROOT", tmp_path / "checkpoints")
    yield


def test_snapshot_guarda_copia_e_indice(tmp_path):
    f = tmp_path / "nota.txt"
    f.write_text("v1", encoding="utf-8")

    cid = cp.snapshot("s1", f)

    assert cid
    assert (tmp_path / "checkpoints" / "s1" / cid).read_text(encoding="utf-8") == "v1"
    items = cp.list_checkpoints("s1")
    assert len(items) == 1
    assert items[0]["id"] == cid
    assert items[0]["path"] == str(f)


def test_snapshot_archivo_inexistente_no_crea_nada(tmp_path):
    assert cp.snapshot("s1", tmp_path / "no-existe.txt") is None
    assert cp.list_checkpoints("s1") == []


def test_dos_snapshots_en_el_mismo_segundo_no_colisionan(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("uno", encoding="utf-8")
    c1 = cp.snapshot("s1", f)
    f.write_text("dos", encoding="utf-8")
    c2 = cp.snapshot("s1", f)

    assert c1 and c2 and c1 != c2
    ids = [c["id"] for c in cp.list_checkpoints("s1")]
    assert ids == [c1, c2]
    base = tmp_path / "checkpoints" / "s1"
    assert (base / c1).read_text(encoding="utf-8") == "uno"
    assert (base / c2).read_text(encoding="utf-8") == "dos"


def test_reloj_repetido_no_pisa_el_checkpoint_anterior(tmp_path, monkeypatch):
    """Con el reloj congelado el id igual tiene que ser unico.

    En Windows `time_ns()` puede devolver el mismo valor en llamadas seguidas:
    antes, dos snapshots del mismo archivo en el mismo tick compartian nombre y
    el segundo borraba el primero (undo intermedio perdido). Este test congela
    el reloj a proposito; es el que fallaba de forma intermitente en la suite.
    """
    monkeypatch.setattr(cp.time, "time_ns", lambda: 1_700_000_000_123_456_789)
    monkeypatch.setattr(cp.time, "strftime", lambda *a, **k: "20260101_000000")

    f = tmp_path / "a.txt"
    ids = []
    for i in range(3):
        f.write_text(str(i), encoding="utf-8")
        ids.append(cp.snapshot("s1", f))

    assert len(set(ids)) == 3
    base = tmp_path / "checkpoints" / "s1"
    # Los tres archivos siguen ahi, con su contenido original.
    assert [ (base / i).read_text(encoding="utf-8") for i in ids ] == ["0", "1", "2"]


def test_restore_vuelve_al_contenido_previo(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("original", encoding="utf-8")
    cid = cp.snapshot("s1", f)
    f.write_text("modificado", encoding="utf-8")

    res = cp.restore("s1", cid)

    assert res == {"ok": True, "restored": str(f)}
    assert f.read_text(encoding="utf-8") == "original"


def test_restore_id_desconocido_devuelve_error(tmp_path):
    assert cp.restore("s1", "nope")["ok"] is False


def test_restore_no_revienta_si_el_original_desaparecio(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    cid = cp.snapshot("s1", f)
    f.unlink()
    res = cp.restore("s1", cid)
    assert res["ok"] is True  # shutil recrea el archivo, no falla
    assert f.read_text(encoding="utf-8") == "x"


def test_list_corta_a_los_ultimos_n(tmp_path):
    f = tmp_path / "a.txt"
    for i in range(5):
        f.write_text(str(i), encoding="utf-8")
        cp.snapshot("s1", f)
    items = cp.list_checkpoints("s1", limit=2)
    assert len(items) == 2
    base = tmp_path / "checkpoints" / "s1"
    assert [ (base / c["id"]).read_text(encoding="utf-8") for c in items ] == ["3", "4"]


def test_diff_muestra_el_cambio(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("linea uno\nlinea dos\n", encoding="utf-8")
    cid = cp.snapshot("s1", f)
    f.write_text("linea uno\nlinea DOS\n", encoding="utf-8")

    res = checkpoint_diff("s1", cid)

    assert res["ok"] is True
    assert res["path"] == str(f)
    assert "-linea dos" in res["diff"]
    assert "+linea DOS" in res["diff"]


def test_diff_checkpoint_inexistente(tmp_path):
    assert checkpoint_diff("s1", "nope")["ok"] is False


def test_unified_sin_cambios_es_vacio():
    assert unified("a\nb\n", "a\nb\n") == ""


def test_sesiones_estan_aisladas(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    cp.snapshot("s1", f)
    assert cp.list_checkpoints("s2") == []
