"""Contrato del body JSON: un JSON valido que no es objeto tambien es 400.

Barrido de las 27 rutas de escritura sin path params con cuerpos raros. Todas
respondian bien a `sin cuerpo`, `cuerpo vacio` y `json invalido` (400), pero
**21 devolvian 500** cuando el JSON era valido y no era un objeto:

    POST /api/plan   {"..."}          -> 200/400 correcto
    POST /api/plan   [1,2,3]          -> 500 Internal Server Error
    POST /api/plan   "hola"           -> 500
    POST /api/plan   null             -> 500

Causa: `_json_body` solo atrapaba los errores de parseo; `[1,2,3]` parsea bien y
despues `body.get(...)` revienta con AttributeError. Como todos los bodies pasan
por ahi (42 endpoints), el chequeo va en el helper: 400 `body debe ser un objeto
JSON`, igual que ya hacia `/api/embed`.

Cuatro endpoints parseaban aparte (`PUT /api/providers/config`,
`POST /api/providers/test`, `PUT /api/fs/write`, `POST /api/exec`) y tenian el
mismo 500: ahora usan el helper.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.api.server import app

client = TestClient(app)

NO_OBJETOS = [b"[1,2,3]", b'"hola"', b"null", b"42", b"true"]


# (metodo, ruta): rutas que se pueden golpear con un cuerpo invalido sin mutar
# nada, porque validan el body antes de tocar el disco, el repo o la config.
RUTAS_ESCRITURA = [
    ("post", "/api/plan"),
    ("post", "/api/diff"),
    ("post", "/api/todos"),
    ("post", "/api/slash"),
    ("put", "/api/keys"),
    ("put", "/api/providers/config"),
    ("post", "/api/providers/test"),
    ("put", "/api/fs/write"),
    ("post", "/api/exec"),
    ("post", "/api/worktree/remove"),
    ("post", "/api/permission/level"),
    ("post", "/api/board/tasks"),
    ("post", "/api/learn/direct"),
    ("post", "/api/agents"),
    ("post", "/api/roundtable"),
    ("post", "/api/voice/tts"),
    ("post", "/api/rag/index-graph"),
]


@pytest.mark.parametrize("metodo,ruta", RUTAS_ESCRITURA)
@pytest.mark.parametrize("cuerpo", NO_OBJETOS, ids=lambda b: b.decode()[:14])
def test_json_que_no_es_objeto_no_es_500(metodo, ruta, cuerpo):
    r = getattr(client, metodo)(ruta, content=cuerpo,
                                headers={"Content-Type": "application/json"})

    assert r.status_code < 500, f"{metodo} {ruta} con {cuerpo!r} -> {r.status_code} {r.text[:200]}"
    assert r.status_code == 400, f"{metodo} {ruta} con {cuerpo!r} -> {r.status_code}"


@pytest.mark.parametrize("metodo,ruta", [
    ("post", "/api/exec"), ("put", "/api/fs/write"), ("post", "/api/plan"),
    ("put", "/api/providers/config"), ("post", "/api/providers/test"),
])
def test_el_detalle_dice_que_se_esperaba_un_objeto(metodo, ruta):
    r = getattr(client, metodo)(ruta, content=b"[1,2,3]",
                                headers={"Content-Type": "application/json"})

    assert r.status_code == 400
    assert "objeto JSON" in r.json()["detail"], r.json()


def test_el_json_invalido_sigue_siendo_400_invalid_json():
    r = client.post("/api/plan", content=b"{ esto no es json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid json"


def test_un_objeto_valido_sigue_pasando():
    """El chequeo no puede romper el caso normal."""
    r = client.post("/api/slash", json={"command": "/help", "ctx": {}})
    assert r.status_code == 200, r.text[:200]
    assert "unknown slash" not in (r.json().get("message") or "")


def test_embed_acepta_objeto_y_rechaza_lista():
    """El precedente que ya estaba bien: /api/embed chequeaba isinstance."""
    assert client.post("/api/embed", json={"text": "hola"}).status_code in (200, 400, 503)
    r = client.post("/api/embed", content=b"[1,2,3]",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "objeto JSON" in r.json()["detail"]
