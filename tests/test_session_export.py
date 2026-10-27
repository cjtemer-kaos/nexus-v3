"""Export de sesion por HTTP: 404 real y nombre de archivo saneado.

`GET /api/sessions/<id>/export` no validaba la sesion: para un id inexistente
devolvia 200 con un ZIP que adentro tiene `<id>.json` sin mensajes. La UI
descargaba un archivo que parece valido y esta vacio, y el cliente nunca se
enteraba de que perdio el id (mismo criterio que /messages y /tree, que ya son
404).

Ademas el `session_id` viajaba crudo al nombre del entry del ZIP y al header
`Content-Disposition`: un id con comillas rompia el header (500) y uno con
barras nombraba `../../x.zip` adentro del zip.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.core.session_log import Message, SessionLog


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    from src.api import server

    log = SessionLog(str(tmp_path / "session.db"))
    monkeypatch.setitem(server._state, "session_log", log)
    try:
        yield TestClient(server.app), log
    finally:
        log.close()


def test_export_de_sesion_inexistente_es_404(cliente):
    http, _ = cliente
    r = http.get("/api/sessions/no-existe/export")
    assert r.status_code == 404, r.text[:200]
    assert "no-existe" in r.json()["detail"]


def test_export_incluye_los_mensajes_reales(cliente):
    http, log = cliente
    log.create_session(project="t", session_id="abc123")
    log.append("abc123", Message(role="user", content="hola export"))

    r = http.get("/api/sessions/abc123/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    assert 'filename="abc123.zip"' in r.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        nombres = z.namelist()
        assert nombres == ["abc123.json"]
        datos = json.loads(z.read("abc123.json").decode("utf-8"))
    assert datos["session_id"] == "abc123"
    assert [m["content"] for m in datos["messages"]] == ["hola export"]
    assert datos["messages"][0]["role"] == "user"


def test_export_sin_mensajes_no_revienta(cliente):
    http, log = cliente
    log.create_session(project="t", session_id="vacia")

    r = http.get("/api/sessions/vacia/export")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        datos = json.loads(z.read("vacia.json").decode("utf-8"))
    assert datos["messages"] == []


def test_export_sanea_el_nombre_del_archivo(cliente):
    """Comillas y barras en el id no pueden romper el header ni el entry del zip."""
    http, log = cliente
    raro = 'a"b c;d'
    log.create_session(project="t", session_id=raro)

    r = http.get(f"/api/sessions/{raro}/export")
    assert r.status_code == 200, r.text[:200]

    cd = r.headers["content-disposition"]
    assert cd.count('"') == 2, f"comillas sin sanear: {cd}"
    assert ";" not in cd.split("filename=")[1]

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        (nombre,) = z.namelist()
        assert "/" not in nombre and "\\" not in nombre and '"' not in nombre
        datos = json.loads(z.read(nombre).decode("utf-8"))
    # el id original no se pierde: viaja adentro del JSON
    assert datos["session_id"] == raro
