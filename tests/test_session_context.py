"""Panel de contexto: desglose real antes de que el request se arme.

El usuario veía "compactó" sin saber qué lo había llenado. El endpoint tiene
que reflejar la forma exacta del próximo request: prompt pineado, resumen,
ventana de 20 mensajes y schemas de tools.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.server import _session_log, app
from src.core.session_log import Message

client = TestClient(app)


def _sesion(project="pytest-context"):
    return _session_log().create_session(project=project).id


def test_contexto_sin_pineo_lo_dice():
    log = _session_log()
    sid = _sesion()
    log.append(sid, Message(role="user", content="hola " * 40))
    log.append(sid, Message(role="assistant", content="respuesta " * 40))

    r = client.get(f"/api/sessions/{sid}/context")
    assert r.status_code == 200
    d = r.json()
    assert d["pinned"] is False and d["tools"] == []
    assert d["breakdown"]["system"] == 0 and d["breakdown"]["tools"] == 0
    assert d["breakdown"]["user"] > 0 and d["breakdown"]["assistant"] > 0
    assert d["used"] == sum(d["breakdown"].values())
    assert d["limit"] > 0 and d["compact_at"] == int(d["limit"] * d["threshold"])
    assert "sin pineo" in d["note"]


def test_contexto_con_pin_desglosa_prompt_y_tools():
    sid = _sesion()
    # Un intercambio previo: el pin recién manda con historial.
    log = _session_log()
    log.append(sid, Message(role="user", content="primer pedido"))
    log.append(sid, Message(role="assistant", content="primera respuesta"))
    pin = client.post(f"/api/sessions/{sid}/pin",
                      json={"system_prompt": "Sos un agente de prueba. " * 20,
                            "tools": ["echo"]})
    assert pin.status_code == 200, pin.text
    assert pin.json()["tools"] == ["echo"]

    d = client.get(f"/api/sessions/{sid}/context").json()
    assert d["pinned"] is True and d["tools"] == ["echo"]
    assert d["breakdown"]["system"] > 0 and d["breakdown"]["tools"] > 0
    assert d["note"] == ""
    assert d["used"] == sum(d["breakdown"].values())
    assert d["pct"] == round(d["used"] / d["limit"], 4)


def test_contexto_solo_cuenta_la_ventana_de_20():
    log = _session_log()
    sid = _sesion()
    for i in range(25):
        log.append(sid, Message(role="user", content=f"mensaje {i} " * 10))

    d = client.get(f"/api/sessions/{sid}/context").json()
    assert d["messages_total"] == 25
    assert d["messages_replayed"] == 20 and d["window"] == 20
    assert d["breakdown"]["user"] > 0
    # Lo pesado se ordena de mayor a menor para saber qué recortar.
    tokens = [h["tokens"] for h in d["heaviest"]]
    assert tokens == sorted(tokens, reverse=True)
    assert len(d["heaviest"]) <= 5


def test_contexto_pin_inexistente_es_404():
    assert client.get("/api/sessions/no-existe/context").status_code == 404
    assert client.get("/api/sessions/no-existe/pin").status_code == 404
    assert client.post("/api/sessions/no-existe/pin",
                       json={"system_prompt": "x"}).status_code == 404
    assert client.delete("/api/sessions/no-existe/pin").status_code == 404


def test_pin_pide_prompt_y_rechaza_tools_inexistentes():
    sid = _sesion()
    assert client.post(f"/api/sessions/{sid}/pin", json={}).status_code == 400
    r = client.post(f"/api/sessions/{sid}/pin",
                    json={"system_prompt": "x", "tools": ["no_existe_esta_tool"]})
    assert r.status_code == 400 and "no_existe_esta_tool" in r.text
    assert client.get(f"/api/sessions/{sid}/pin").json()["pinned"] is False
