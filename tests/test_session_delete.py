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
    import sqlite3

    from src.api.server import _session_log

    client = TestClient(app)
    sid = client.post("/api/sessions", json={"project": "pytest-delete"}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"role": "user", "content": "hola borrado"})
    assert client.get(f"/api/sessions/{sid}/messages").json()["count"] >= 1
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    # La sesion ya no existe: el endpoint responde 404 (igual que /tree y /fork),
    # no un 200 vacio que el cliente confundiria con una sesion nueva.
    assert client.get(f"/api/sessions/{sid}/messages").status_code == 404
    # El objetivo real del test: no quedan mensajes huerfanos tras el cascade.
    slog = _session_log()
    con = sqlite3.connect(str(slog.db_path))
    try:
        huerfanos = con.execute(
            "SELECT count(*) FROM messages WHERE session_id=?", (sid,)
        ).fetchone()[0]
    finally:
        con.close()
    assert huerfanos == 0
