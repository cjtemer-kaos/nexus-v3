"""Borrado de sesiones: DELETE /api/sessions/{id} (cascade mensajes + FTS)."""
from fastapi.testclient import TestClient

from src.api.server import app


def test_delete_session_flow():
    client = TestClient(app)
    # crear
    r = client.post("/api/sessions", json={"project": "pytest-delete"})
    assert r.status_code == 200
    sid = r.json()["id"]
    # aparece en el listado
    listing = client.get("/api/sessions?limit=500").json()["sessions"]
    assert any(s["id"] == sid for s in listing)
    # borrar
    d = client.delete(f"/api/sessions/{sid}")
    assert d.status_code == 200
    assert d.json()["deleted"] is True
    # ya no esta
    listing2 = client.get("/api/sessions?limit=500").json()["sessions"]
    assert not any(s["id"] == sid for s in listing2)
    # segundo borrado -> 404
    d2 = client.delete(f"/api/sessions/{sid}")
    assert d2.status_code == 404


def test_delete_session_cascades_messages():
    client = TestClient(app)
    sid = client.post("/api/sessions", json={"project": "pytest-delete"}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"role": "user", "content": "hola borrado"})
    assert client.get(f"/api/sessions/{sid}/messages").json()["count"] >= 1
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    # la sesion ya no existe -> el endpoint de mensajes devuelve vacio/0
    assert client.get(f"/api/sessions/{sid}/messages").json()["count"] == 0
