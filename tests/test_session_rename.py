"""Renombrar una sesión por HTTP (`PATCH /api/sessions/<id>`).

El título se generaba solo con el primer mensaje y no había forma de corregirlo
desde la UI. El endpoint es el único campo editable, así que los bordes importan:
un título vacío no puede borrar el que había, y un id inexistente no puede
devolver 200 (la UI lo mostraría como renombrado y al recargar volvería atrás).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.core.session_log import SessionLog


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    from src.api import server

    log = SessionLog(str(tmp_path / "session.db"))
    monkeypatch.setitem(server._state, "session_log", log)
    try:
        yield TestClient(server.app), log
    finally:
        log.close()


def test_renombra_y_persiste(cliente):
    http, log = cliente
    log.create_session(project="t", session_id="abc123")

    r = http.patch("/api/sessions/abc123", json={"title": "  Mi sesión  "})
    assert r.status_code == 200, r.text[:200]
    assert r.json() == {"ok": True, "id": "abc123", "title": "Mi sesión"}

    ses = log.get_session("abc123")
    assert ses is not None and ses.title == "Mi sesión"


def test_titulo_vacio_es_400_y_no_toca_el_anterior(cliente):
    http, log = cliente
    log.create_session(project="t", session_id="abc123")
    log.update_session("abc123", title="Título bueno")

    for malo in ("", "   ", None):
        r = http.patch("/api/sessions/abc123", json={"title": malo})
        assert r.status_code == 400, f"{malo!r} -> {r.status_code}"
    assert log.get_session("abc123").title == "Título bueno"


def test_sesion_inexistente_es_404(cliente):
    http, _ = cliente
    r = http.patch("/api/sessions/no-existe", json={"title": "x"})
    assert r.status_code == 404
    assert "no-existe" in r.json()["detail"]


def test_titulo_largo_se_recorta(cliente):
    http, log = cliente
    log.create_session(project="t", session_id="abc123")

    r = http.patch("/api/sessions/abc123", json={"title": "x" * 500})
    assert r.status_code == 200
    assert len(r.json()["title"]) == 200
    assert len(log.get_session("abc123").title) == 200


def test_json_invalido_es_400(cliente):
    http, _ = cliente
    r = http.patch("/api/sessions/abc123", content=b"{no json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
