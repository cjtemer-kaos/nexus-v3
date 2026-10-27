"""Ciclo real de checkpoints por HTTP: snapshot -> diff -> restore.

En disco no habia NINGUN checkpoint, asi que el camino feliz (crear, ver el
diff, restaurar, y que el archivo vuelva a su contenido anterior) nunca se
habia ejercitado: solo se probaban los errores (id inexistente, sin id).
Aca se usa un `_ROOT` temporal para no tocar `data/checkpoints` del usuario.
"""
from __future__ import annotations

import pytest

from src.core import checkpoints as cp


@pytest.fixture()
def cp_root(tmp_path, monkeypatch):
    root = tmp_path / "checkpoints"
    root.mkdir()
    monkeypatch.setattr(cp, "_ROOT", root)
    return root


@pytest.fixture()
def archivo(tmp_path):
    p = tmp_path / "nota.txt"
    p.write_text("linea uno\nlinea dos\n", encoding="utf-8")
    return p


def _client():
    from fastapi.testclient import TestClient

    from src.api.server import app

    return TestClient(app)


def test_flujo_completo_snapshot_diff_restore(cp_root, archivo):
    cid = cp.snapshot("s1", archivo)
    assert cid

    archivo.write_text("linea uno\nCAMBIO\nlinea tres\n", encoding="utf-8")

    cliente = _client()

    r = cliente.get("/api/checkpoints?session_id=s1")
    assert r.status_code == 200
    items = r.json()["checkpoints"]
    assert [i["id"] for i in items] == [cid]
    assert items[0]["path"] == str(archivo)

    r = cliente.get(f"/api/checkpoints/{cid}/diff?session_id=s1")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["path"] == str(archivo)
    assert "-linea dos" in body["diff"]
    assert "+CAMBIO" in body["diff"]
    assert "+linea tres" in body["diff"]

    r = cliente.post("/api/checkpoints/restore",
                     json={"session_id": "s1", "checkpoint_id": cid})
    assert r.status_code == 200, r.text
    assert r.json()["restored"] == str(archivo)
    assert archivo.read_text(encoding="utf-8") == "linea uno\nlinea dos\n"

    # despues de restaurar, el diff contra el archivo actual queda vacio
    r = cliente.get(f"/api/checkpoints/{cid}/diff?session_id=s1")
    assert r.json()["ok"] is True
    assert r.json()["diff"] == ""


def test_restore_recrea_archivo_borrado(cp_root, archivo):
    cid = cp.snapshot("s1", archivo)
    archivo.unlink()
    assert not archivo.exists()

    r = _client().post("/api/checkpoints/restore",
                       json={"session_id": "s1", "checkpoint_id": cid})
    assert r.status_code == 200, r.text
    assert archivo.read_text(encoding="utf-8") == "linea uno\nlinea dos\n"


def test_diff_con_archivo_borrado_no_revienta(cp_root, archivo):
    cid = cp.snapshot("s1", archivo)
    archivo.unlink()

    r = _client().get(f"/api/checkpoints/{cid}/diff?session_id=s1")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "-linea uno" in body["diff"]


def test_listados_por_sesion_no_se_mezclan(cp_root, archivo, tmp_path):
    otro = tmp_path / "otro.txt"
    otro.write_text("otro\n", encoding="utf-8")

    a = cp.snapshot("s1", archivo)
    b = cp.snapshot("s2", otro)

    cliente = _client()
    ids1 = {c["id"] for c in cliente.get("/api/checkpoints?session_id=s1").json()["checkpoints"]}
    ids2 = {c["id"] for c in cliente.get("/api/checkpoints?session_id=s2").json()["checkpoints"]}
    assert ids1 == {a}
    assert ids2 == {b}

    # un checkpoint de otra sesion no se puede restaurar ni ver
    r = cliente.get(f"/api/checkpoints/{b}/diff?session_id=s1")
    assert r.json()["ok"] is False
    r = cliente.post("/api/checkpoints/restore",
                     json={"session_id": "s1", "checkpoint_id": b})
    assert r.status_code == 404


def test_snapshot_ignora_archivo_inexistente_y_directorios(cp_root, tmp_path):
    assert cp.snapshot("s1", tmp_path / "no-existe.txt") is None
    d = tmp_path / "carpeta"
    d.mkdir()
    assert cp.snapshot("s1", d) is None
    assert cp.list_checkpoints("s1") == []


def test_dos_snapshots_del_mismo_archivo_no_se_pisan(cp_root, archivo):
    a = cp.snapshot("s1", archivo)
    b = cp.snapshot("s1", archivo)
    assert a and b and a != b
    d = cp._session_dir("s1")
    assert (d / a).is_file()
    assert (d / b).is_file()
    assert len(cp.list_checkpoints("s1")) == 2


def test_restore_con_id_de_traversal_no_escapa(cp_root, archivo, tmp_path):
    cp.snapshot("s1", archivo)
    fuera = tmp_path / "secreto.txt"
    fuera.write_text("no me pises\n", encoding="utf-8")

    for malo in ["../secreto.txt", "..\\secreto.txt", "../../etc/passwd"]:
        r = _client().post("/api/checkpoints/restore",
                           json={"session_id": "s1", "checkpoint_id": malo})
        assert r.status_code == 404, (malo, r.status_code, r.text)
    assert fuera.read_text(encoding="utf-8") == "no me pises\n"


def test_snapshot_vacio_no_inventa_checkpoint(cp_root, tmp_path):
    vacio = tmp_path / "vacio.txt"
    vacio.write_text("", encoding="utf-8")
    cid = cp.snapshot("s1", vacio)
    assert cid
    assert (cp._session_dir("s1") / cid).is_file()
